# Investigation: OIDC Authentication Mapping After Plugin Install

Date: 2026-08-04

## Problem Statement

A customer reported that after installing the BeyondTrust credential plugin, their AAP authentication mapping silently breaks. Specifically:

- The AAP gateway still successfully authenticates the user via OIDC
- The `is_superuser` controller permission attribute that was being applied via a group-based authenticator map **stops being applied** after the credential plugin is installed

## Test Environment

- AAP 2.7 containerized on RHEL (`aap27growth.lab.cheeseburgia.com`)
- Keycloak 26.7.0 on OpenShift (ROSA) as OIDC provider
- Controller image: `quay.io/aap/ansible-automation-platform-27-next/controller-rhel9:latest`

## Reproduction Steps

### 1. Baseline (stock image)

- Deployed Keycloak with:
  - Realm: `aap`
  - Client: `aap-controller` (confidential, OIDC)
  - Group: `superuser-group`
  - User: `testuser` (member of `superuser-group`)
  - Groups mapper on client (claim name: `groups`)
- Configured AAP gateway with:
  - OIDC authenticator pointed at Keycloak
  - Authenticator map: `is_superuser` triggered by `groups` claim containing `superuser-group`
- Tested OIDC login as `testuser`
- **Result: `is_superuser: True`** — mapping works correctly

### 2. After BeyondTrust plugin install

- Switched controller containers to custom image with BeyondTrust plugin
- Ran `awx-manage setup_managed_credential_types`
- Deleted `testuser` to force fresh user creation on next login
- Repeated OIDC login as `testuser`
- **Result: `is_superuser: True`** — mapping still works

### 3. After container restart

- Restarted all three controller containers
- Repeated OIDC login
- **Result: `is_superuser: True`** — mapping still works

## Conclusion

**The issue does NOT reproduce in our environment.**

## Key Architectural Finding

Authentication mapping in AAP 2.7 is handled entirely by the **gateway** service, not the controller:

```
Gateway container (unchanged)          Controller container (modified)
├── OIDC authentication                ├── Credential plugins
├── Authenticator maps                 ├── Job execution
├── User attribute assignment          └── awx-manage commands
└── Session management
```

The credential plugin only modifies the controller container. The gateway container — which owns the entire authentication and mapping pipeline — is never touched during plugin installation.

`awx-manage setup_managed_credential_types` only inserts/updates a `CredentialType` row in the controller's database. It does not:
- Touch the gateway database
- Modify authenticator maps
- Affect user attributes
- Trigger migrations that could impact auth

## Possible Explanations for Customer's Issue

Since we cannot reproduce, the customer's problem likely stems from one of these scenarios:

1. **Gateway container was inadvertently restarted or recreated** during the installation process, causing a state reset or configuration loss

2. **The customer modified the gateway image** as well (e.g., misunderstanding which container needs the plugin), which broke the social auth pipeline

3. **Container recreation caused a networking disruption** between gateway and controller, causing the gateway's post-auth sync with the controller to fail silently (user gets created/authenticated but the `is_superuser` flag fails to propagate to the controller's user record)

4. **Different auth map configuration** — the customer may be using organization/team-level mappings that rely on controller-side resources, rather than the simpler `is_superuser` flag. If `setup_managed_credential_types` triggers any model change that invalidates cached org/team lookups, those maps could fail

5. **Timing/ordering issue** — if the customer's process involves running the full AAP installer (not just rebuilding the controller image), the installer may reset or overwrite gateway configuration

## Recommendations for Customer

1. Confirm that **only** the controller containers were modified — the gateway must remain untouched
2. Check if their auth map uses `is_superuser` (which is gateway-only) or organization/team mappings (which involve the controller)
3. Ask them to compare `curl -u admin:<pass> https://localhost/api/gateway/v1/authenticator_maps/` output before and after the plugin installation
4. If the issue is intermittent, it may be a race condition during container restart where the gateway tries to sync user attributes with a controller that isn't ready yet

## Image Tagging Strategy Used

For easy switching between stock and custom images:

```bash
# Tag stock image for reference
podman tag <stock-id> localhost/controller-rhel9:stock

# Tag custom image
podman tag <custom-id> localhost/controller-rhel9:beyondtrust

# Switch to stock
podman tag localhost/controller-rhel9:stock quay.io/aap/.../controller-rhel9:latest

# Switch to beyondtrust
podman tag localhost/controller-rhel9:beyondtrust quay.io/aap/.../controller-rhel9:latest

# Then recreate containers
```
