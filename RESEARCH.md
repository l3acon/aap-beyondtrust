# AAP Custom Credential Plugin for BeyondTrust Password Safe

## Overview

This document outlines how to implement a **custom credential plugin** for Ansible Automation Platform (AAP) 2.7 (containerized on RHEL) that retrieves secrets from BeyondTrust Password Safe at **job launch time** (server-side on the controller), rather than using a lookup plugin that executes within a playbook on endpoints.

## How AAP Credential Plugins Work

### Architecture

AAP credential plugins are **Python packages** registered via setuptools entry points. When a job template runs, AAP's controller resolves credential fields *before* the playbook executes — the secret is fetched server-side and injected into the job environment (as env vars, extra vars, or files).

The flow:
1. User creates a **source credential** (the BeyondTrust credential with connection info)
2. User creates a **target credential** (e.g., a Machine credential for SSH)
3. User **links** a field on the target credential (e.g., `password`) to the source credential, providing metadata (e.g., which managed account to look up)
4. At job launch, AAP calls the plugin's `backend` function with the inputs + metadata
5. The plugin calls the BeyondTrust API and returns the secret value
6. AAP injects the resolved value into the job

### Important: Controller Image vs. Execution Environment

Credential plugins run inside the **controller's Django process**, NOT inside an Execution Environment (EE). EEs are where playbooks execute on managed hosts. The credential resolution happens *before* the EE is even launched.

This means the plugin must be installed into the **automation controller container image** itself. You cannot solve this with `execution-environment.yml` or `ansible-builder` alone — you need a custom **Containerfile** that extends the controller image.

### Plugin Structure

A credential plugin is a Python object (namedtuple or class) with three attributes:

| Attribute | Purpose |
|-----------|---------|
| `name` | Display name shown in the AAP UI |
| `inputs` | Dict defining `fields` (stored config) and `metadata` (per-link config) |
| `backend` | Callable that receives all inputs+metadata and returns the secret value |

**Entry point group** (AAP 2.7 / modern AWX):
```
awx_plugins.credentials
```

**Entry point group** (legacy AAP < 2.5):
```
awx.credential_plugins
```

> For AAP 2.7 containerized, determine which group is active by inspecting the installed `awx-plugins-core` package inside the running controller container. If both groups are supported, prefer `awx_plugins.credentials`.

## BeyondTrust Password Safe API Flow

The BeyondTrust REST API requires a multi-step workflow to retrieve a credential:

```
1. POST /Auth/SignAppin           → Authenticate, get session
2. GET  /ManagedAccounts?systemName={system}&accountName={account}  → Get account ID
3. POST /Requests                 → Request credential checkout
4. GET  /Credentials/{requestId}  → Retrieve the actual password
5. PUT  /Requests/{requestId}/Checkin  → Release the credential
6. POST /Auth/Signout             → End session
```

## Plugin Implementation

The plugin source lives in this repository. Key files:

```
aap-beyondtrust/
├── pyproject.toml                              # Package config + entry points (both groups)
├── Containerfile                               # Extends the AAP controller image
├── .dockerignore
├── RESEARCH.md
└── src/
    └── beyondtrust_credential_plugin/
        ├── __init__.py                         # Exports beyondtrust_plugin
        └── plugin.py                           # Inputs, backend function, caching
```

See `src/beyondtrust_credential_plugin/plugin.py` for the full implementation. Key design decisions:

- **Registers under both entry point groups** (`awx_plugins.credentials` and `awx.credential_plugins`) for forward/backward compatibility
- **Deferred `requests` import** inside the backend function (follows OneIdentity pattern for graceful error messages)
- **Thread-safe in-memory cache** (30s TTL) so multiple field lookups against the same managed account reuse the same credential checkout
- **String-to-bool handling** for `verify_ssl` since AWX UI may pass `"true"`/`"false"` strings
- **Proper checkin + signout** in finally/except blocks so credentials don't stay checked out on errors

## Deployment: Custom Controller Container Image

Since AAP 2.7 on RHEL runs the controller as an immutable Podman container, the plugin must be baked into a custom controller image.

### Step 1: Identify the Base Controller Image

The default controller image for our AAP 2.7 environment is:
```
quay.io/aap/ansible-automation-platform-27-next/controller-rhel9:latest
```

Verify the exact image/tag in use on your system:
```bash
podman ps --format '{{.Image}}' | grep controller
```

### Step 2: Build and Push the Image

The `Containerfile` in this repo handles the build. Replace the registry path with your internal registry:

```bash
podman build -t registry.internal.example.com/aap/controller-beyondtrust:latest .
podman push registry.internal.example.com/aap/controller-beyondtrust:latest
```

### Step 3: Configure AAP Installer to Use the Custom Image

In the AAP containerized installer inventory file, override the controller image:

```ini
[all:vars]
# ... existing vars ...
controller_image=registry.internal.example.com/aap/controller-rhel9-beyondtrust:latest
```

Then re-run the installer playbook:
```bash
ansible-playbook -i inventory containerized_installer.yml
```

### Step 4: Register the Plugin

After the container is running with the custom image, exec into it and register:
```bash
podman exec -it <controller-container-name> awx-manage setup_managed_credential_types
```

Then restart the controller container:
```bash
podman restart <controller-container-name>
```

## Usage in AAP After Installation

1. **Create a BeyondTrust credential**: Navigate to Credentials → Add → select "BeyondTrust Password Safe Lookup" type → provide URL, API key, and API user
2. **Create/edit a target credential** (e.g., Machine credential): Click the key icon next to the `password` field → select your BeyondTrust credential → provide metadata (system name, account name)
3. **Attach the target credential to a Job Template**: At job launch, AAP fetches the password from BeyondTrust and injects it

## Key Considerations

### Entry Point Group Discovery

Before building, confirm which entry point group your AAP 2.7 installation scans. Exec into the running controller container and check:
```bash
podman exec -it <controller-container> python3 -c "
import importlib.metadata
# Modern group
eps = importlib.metadata.entry_points(group='awx_plugins.credentials')
print('awx_plugins.credentials:', [ep.name for ep in eps])
# Legacy group
eps2 = importlib.metadata.entry_points(group='awx.credential_plugins')
print('awx.credential_plugins:', [ep.name for ep in eps2])
"
```

If your installation only reads the legacy group, update `pyproject.toml` to register under `awx.credential_plugins` instead.

### Caching

Each linked field triggers a separate call to the backend function. If you're retrieving username AND password from the same BeyondTrust account, the plugin will be called twice. Consider implementing a short-lived cache (keyed on system+account) to avoid redundant API calls and checkout requests.

### Error Handling

- Handle BeyondTrust API errors gracefully (auth failures, account not found, request denied)
- Consider timeout settings on the requests
- Handle cases where credential checkout requires approval (the API may not return immediately)

### Security

- The API key is stored encrypted in the AAP database (marked `secret: True`)
- The plugin runs on the controller node, not on managed hosts
- Ensure the BeyondTrust API user has minimal required permissions
- Consider certificate-based auth if your BeyondTrust instance supports it

### BeyondTrust Configuration Prerequisites

- An API Registration must exist in BeyondInsight for AAP
- The API user must have permissions to request credentials for the target managed accounts
- Auto-approve policies should be configured for the AAP API user (otherwise checkout requests will pend for approval and the plugin will time out)
- Network connectivity from the AAP controller container to the BeyondTrust API endpoint

### Upgrade Path

When upgrading AAP, the base controller image will change. You must:
1. Rebuild the custom image against the new base image tag
2. Push to your registry
3. Re-run the installer

Automate this with a CI pipeline that triggers on AAP version updates.

## Lessons from OneIdentity Safeguard Plugin

The [OneIdentity/safeguard-ansible](https://github.com/OneIdentity/safeguard-ansible/tree/main/credential_type_plugin) project implemented an equivalent credential plugin for their PAM product (Safeguard for Privileged Passwords). Key takeaways:

### They use the legacy entry point group

Their `pyproject.toml` registers under `awx.credential_plugins` (not `awx_plugins.credentials`):
```toml
[project.entry-points."awx.credential_plugins"]
spp_plugin = "safeguardcredentialtype:spp_plugin"
```
This confirms that even relatively recent plugin development targets the legacy group. We should **register under both groups** or confirm which one AAP 2.7 actually scans.

### No metadata fields — everything in `fields`

Their plugin puts ALL configuration (API key, appliance address, cert paths, credential type) into `inputs.fields` with an **empty `metadata: []`**. This means every credential instance is self-contained — you don't specify "which secret" at link time, because the API key already maps 1:1 to a specific credential in Safeguard.

For BeyondTrust this is different — we likely want `metadata` fields (system name, account name) so that a single BeyondTrust source credential can be reused to look up multiple managed accounts.

### Boolean fields passed as strings from the UI

They explicitly handle AWX passing `'true'`/`'false'` strings for boolean-ish fields:
```python
if isinstance(validate_certs, str):
    validate_certs = validate_certs.lower() not in ('false', '0', 'no')
```
We should do the same for our `verify_ssl` field.

### Deferred imports for the SDK dependency

They import `pysafeguard` inside the backend function, not at module level, and raise a clear error if it's missing:
```python
try:
    from pysafeguard import A2AContext, A2AType
except ImportError as e:
    raise ValueError('The pysafeguard package is not installed...') from e
```
This is a good pattern — it prevents the plugin from crashing AWX at startup if the dependency is somehow missing, and gives operators a clear error message.

### Upgrade pain: `KeyError` after AAP upgrades

Their README explicitly warns:
> After upgrading AWX or Ansible Automation Platform, the Python virtual environment may be recreated. If this happens, the plugin must be reinstalled.
> If you see `KeyError: 'spp_plugin'` in the AWX logs after an upgrade, this indicates the plugin is not installed.

For containerized AAP this is less of a concern (the plugin is baked into the image), but it reinforces that our **Containerfile must be rebuilt whenever the base image is updated**. Our CI pipeline should handle this.

### Certificate-based auth vs API key auth

OneIdentity uses **client certificate authentication** (cert + private key files on disk). BeyondTrust uses **API key + RunAs user** authentication via HTTP headers. Our plugin is simpler in this regard — no need to manage cert files on the controller filesystem.

### They wrap a vendor SDK (`pysafeguard`)

Rather than raw HTTP calls, they depend on a vendor-provided Python SDK. BeyondTrust does not appear to offer an official Python SDK for Password Safe, so we'll use `requests` directly against the REST API. This gives us fewer dependencies and more control, but means we own the API interaction code.

## Open Questions / Next Steps

- [x] Confirm the exact base controller image tag → `quay.io/aap/ansible-automation-platform-27-next/controller-rhel9:latest`
- [x] Determine which entry point group is active → `awx_plugins.credentials` (modern group; legacy group is empty)
- [x] Plugin installs into venv at `/var/lib/awx/venv/awx/` (Python 3.12)
- [x] Deployed and registered as credential type ID=33 on test environment
- [ ] Validate BeyondTrust API connectivity from the controller container's network namespace
- [ ] Determine BeyondTrust auto-approve policy configuration for the AAP service account
- [ ] End-to-end test with a real BeyondTrust instance
- [ ] Set up CI to rebuild the custom image on base image updates

## References

- [AWX Credential Plugins Documentation](https://github.com/ansible/awx/blob/devel/docs/credentials/credential_plugins.md)
- [AWX Custom Credential Plugin Example](https://github.com/ansible/awx-custom-credential-plugin-example)
- [AWX Plugins Core (modern plugin architecture)](https://github.com/ansible/awx-plugins)
- [awx-plugins-core RTD](https://awx-plugins-core.readthedocs.io/en/latest/credential-plugins/)
- [OneIdentity Safeguard Credential Plugin](https://github.com/OneIdentity/safeguard-ansible/tree/main/credential_type_plugin) — real-world PAM credential plugin implementation, good reference for patterns and pitfalls
- [BeyondTrust Password Safe REST API](https://docs.beyondtrust.com/bips/v25.2/docs/api)
- [BeyondTrust Secrets Safe REST API](https://docs.beyondtrust.com/bips/v25.1/docs/secrets-safe-apis)
- [BeyondTrust Secrets Safe Ansible Lookup Plugin](https://galaxy.ansible.com/ui/repo/published/beyondtrust/secrets_safe/content/lookup/secrets_safe_lookup/)
- [AAP 2.7 Image Variables](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/install-image_variables)
- [Derek Waters - Building a Custom Credential Plugin](https://derekwaters.github.io/ansible/execution/environments/credentials/aws/sts/assume/role/2023/12/21/building-a-custom-credential-plugin.html)
- [Ansible Forum - Custom credential plugins in containerized AAP](https://forum.ansible.com/t/basic-process-to-install-new-awx-credential-plugins-in-awx/37848)
