# AAP BeyondTrust Secrets Safe Credential Plugin

A custom credential plugin for Ansible Automation Platform (AAP) 2.7 that retrieves secrets from BeyondTrust Secrets Safe **at job launch time** on the controller — not during playbook execution on endpoints.

When a job template runs, AAP resolves linked credential fields server-side before the playbook ever reaches a managed host. This plugin calls the BeyondTrust REST API to retrieve a secret and returns it to AAP for injection into the job environment.

## Prerequisites

- AAP 2.7 deployed on **RHEL** (containerized) or **OpenShift** (operator-managed)
- Network connectivity from the controller to your BeyondTrust Secrets Safe API
- A BeyondTrust API Registration configured for AAP with either:
  - An API key + a RunAs user with permissions to read secrets in the target Secrets Safe folders, or
  - An OAuth Client ID/Secret with equivalent read permissions

### Additional prerequisites by platform

| | RHEL | OpenShift |
|---|---|---|
| **Tool** | Podman on the controller host | `oc` CLI logged into the cluster |
| **Image delivery** | Local podman image store | Container registry accessible to the cluster |
| **Permissions** | AAP service user (non-root) | Ability to patch `AutomationController` CR |

## Deployment

Credential plugins run inside the **controller's Django process**, not in an Execution Environment. The plugin must be baked into a custom controller container image. Only the controller containers are modified — the gateway, hub, EDA, and database containers remain untouched.

### Quick start (one command)

Clone this repo onto the AAP controller host and run:

```bash
git clone https://github.com/l3acon/aap-beyondtrust.git
cd aap-beyondtrust
ansible-playbook install-plugin.yml
```

This will validate your environment, build the custom image, switch the controller, and register the credential type. To uninstall:

```bash
ansible-playbook switch-controller-image.yml -e image_tag=stock
```

### Manual steps

### In-Place Tag Replacement

This approach builds the custom image and tags it with the same name as the original controller image. When the containers are recreated, they pick up the new image automatically.

#### 1. Identify your base controller image

```bash
podman ps --format '{{.Names}} {{.Image}}' | grep controller | grep -v eda
```

Example output:
```
automation-controller-web quay.io/aap/ansible-automation-platform-27-next/controller-rhel9:latest
```

#### 2. Save the stock image for rollback

```bash
CONTROLLER_IMAGE=$(podman inspect automation-controller-web --format '{{.ImageName}}')
STOCK_ID=$(podman inspect automation-controller-web --format '{{.Image}}' | head -c 12)
podman tag $STOCK_ID localhost/controller-rhel9:stock
```

#### 3. Build and tag the custom image

Clone this repo onto the AAP controller host:

```bash
git clone https://github.com/l3acon/aap-beyondtrust.git
cd aap-beyondtrust
podman build -t $CONTROLLER_IMAGE .
podman tag $CONTROLLER_IMAGE localhost/controller-rhel9:beyondtrust
```

#### 4. Recreate the controller containers

Stop, remove, and recreate the three controller containers:

```bash
podman stop automation-controller-web automation-controller-task automation-controller-rsyslog
podman rm automation-controller-web automation-controller-task automation-controller-rsyslog
```

Then re-run the AAP installer to recreate them:

```bash
cd ~/aap-containerized-installer  # your installer bundle location
ansible-playbook -i inventory install.yml
```

If the installer has connectivity issues (common in air-gapped environments), see [Recreating containers without the installer](#recreating-containers-without-the-installer) below.

#### 5. Register the credential type

```bash
podman exec automation-controller-web awx-manage setup_managed_credential_types
```

#### 6. Verify

```bash
podman exec automation-controller-web awx-manage shell -c "
from awx.main.models import CredentialType
ct = CredentialType.objects.get(name__icontains='beyondtrust')
print(f'{ct.name} (ID={ct.id}, kind={ct.kind})')
"
```

Expected output:
```
BeyondTrust Secrets Safe Lookup (ID=33, kind=external)
```

### Switching images with the playbook

This repo includes a playbook (`switch-controller-image.yml`) that handles stopping, removing, and recreating the controller containers with a specified image tag. It does not require the full AAP installer or network access to registries.

To switch to the BeyondTrust image:

```bash
ansible-playbook switch-controller-image.yml -e image_tag=beyondtrust
```

To roll back to the stock image:

```bash
ansible-playbook switch-controller-image.yml -e image_tag=stock
```

The playbook uses locally-tagged images (`localhost/controller-rhel9:<tag>`). The initial build step (step 3 above) creates the `beyondtrust` tag, and step 2 creates the `stock` tag.

After switching to `beyondtrust`, register the credential type:

```bash
podman exec automation-controller-web awx-manage setup_managed_credential_types
```

### Deployment on OpenShift (OCP)

On OpenShift, AAP is managed by the **AutomationController operator**. Instead of manipulating podman containers directly, we patch the `AutomationController` custom resource with a custom image reference. The operator handles the rolling update.

#### Quick start (one command)

If you have `podman` (or `docker`) locally and are logged into the cluster with `oc`:

```bash
ansible-playbook install-plugin-ocp.yml \
  -e custom_image=quay.io/myorg/controller-beyondtrust:latest \
  -e build_and_push=true
```

This will detect the base controller image from the running cluster, build the custom image, push it to your registry, patch the CR, wait for the rollout, and register the credential types.

#### Pre-built image

If the image is already in a registry:

```bash
ansible-playbook install-plugin-ocp.yml \
  -e custom_image=quay.io/myorg/controller-beyondtrust:latest
```

#### With a private registry

If your registry requires authentication and the cluster doesn't already have a pull secret:

```bash
ansible-playbook install-plugin-ocp.yml \
  -e custom_image=registry.internal.example.com/aap/controller-beyondtrust:latest \
  -e build_and_push=true \
  -e create_registry_secret=true \
  -e registry_server=registry.internal.example.com \
  -e registry_username=myuser \
  -e registry_password='<your-registry-password>'
```

#### Targeting a specific namespace

By default the playbook uses the current `oc project`. To target a different namespace:

```bash
ansible-playbook install-plugin-ocp.yml \
  -e custom_image=quay.io/myorg/controller-beyondtrust:latest \
  -e aap_namespace=my-aap-namespace
```

#### Rollback

To revert to the stock controller image:

```bash
ansible-playbook install-plugin-ocp.yml -e rollback=true
```

This removes the `image` override from the `AutomationController` CR. The operator reconciles and reverts to its default image.

#### How it works (OCP)

1. **Queries** the `AutomationController` CR to discover the current base image
2. **Builds** a custom image using the same `Containerfile` as RHEL (if `build_and_push=true`)
3. **Pushes** to the specified registry
4. **Patches** `spec.image` on the `AutomationController` CR
5. The **operator reconciles** and performs a rolling update of the controller Deployments
6. **Registers** credential types via `awx-manage setup_managed_credential_types`
7. **Verifies** the plugin entry points are loaded

## Usage

This plugin provides one credential type in the AAP UI:

- **BeyondTrust Secrets Safe Lookup** — for secrets stored in Secrets Safe folders (direct retrieval)

### Create a source credential

1. In AAP, navigate to **Resources → Credentials → Add**
2. Select credential type: **BeyondTrust Secrets Safe Lookup**
3. Fill in:
   - **BeyondTrust URL** — base API URL (e.g. `https://bt.example.com/BeyondTrust/api/public/v3`)
   - **API Key** + **API User** — OR — **OAuth Client ID** + **OAuth Client Secret**
   - **Verify SSL Certificates** — `true` for production, `false` for lab/self-signed

### Link to a target credential

1. Create or edit a target credential (e.g. a **Machine** credential)
2. Click the key/link icon next to the field you want to populate
3. Select your BeyondTrust Secrets Safe credential as the source
4. Provide the metadata:
   - **Secret Path** — folder path (e.g. `infrastructure/linux`)
   - **Secret Title** — the title of the secret
   - **Secret Field** — which field to return: `password`, `username`, or `text`
   - **Path Separator** — separator character (default: `/`)
5. Save

### Use in a Job Template

Attach the target credential (e.g. Machine credential) to your Job Template as normal. At job launch, AAP will:

1. Call the BeyondTrust API to retrieve the credential
2. Inject the resolved value into the job

The playbook and managed hosts never see the BeyondTrust API — they just receive the resolved credential.

## Project Structure

```
aap-beyondtrust/
├── README.md
├── RESEARCH.md                                 # Design decisions and background research
├── Containerfile                               # Extends the AAP controller image
├── install-plugin.yml                          # One-command install for RHEL (build + deploy)
├── install-plugin-ocp.yml                      # One-command install for OpenShift
├── build-plugin-image.yml                      # Build image only (no deploy, RHEL)
├── switch-controller-image.yml                 # Switch between stock/beyondtrust images (RHEL)
├── switch-controller-tasks.yml                 # Shared tasks for container recreation (RHEL)
├── pyproject.toml                              # Package config + entry points
├── .dockerignore
├── .gitignore
├── docs/
│   └── oidc-auth-mapping-investigation.md      # Auth mapping regression investigation
└── src/
    └── beyondtrust_credential_plugin/
        ├── __init__.py                         # Exports the credential plugin
        ├── _common.py                          # Shared auth, cache, and error handling
        └── secrets_safe.py                     # Secrets Safe credential plugin
```

## How It Works

The plugin authenticates (`POST /Auth/SignAppin`, or an OAuth client-credentials token exchange) and uses a 30-second in-memory cache so that multiple field lookups against the same secret reuse a single API call.

### Secrets Safe flow

1. **Authenticate** — `POST /Auth/SignAppin` (API key + RunAs) or OAuth token exchange
2. **Retrieve secret** — `GET /Secrets-Safe/Secrets?Path=X&Title=Y&Decrypt=true`
3. **Sign out** — `POST /Auth/Signout`

There is no checkout/checkin workflow — secrets are retrieved directly by folder path and title.

## Upgrading AAP

When upgrading AAP to a new version, the base controller image changes. You must rebuild the custom image against the new base:

```bash
podman build --build-arg BASE_IMAGE=<new-base-image> -t <your-image>:latest .
```

Then follow steps 3-4 above to recreate containers and re-register.

## Troubleshooting

### Plugin not appearing in credential type dropdown

Verify the entry point is registered in the running container:

```bash
podman exec automation-controller-web /var/lib/awx/venv/awx/bin/python3 -c "
import importlib.metadata
eps = importlib.metadata.entry_points().select(group='awx_plugins.credentials')
for ep in eps:
    if 'beyondtrust' in ep.name:
        print(f'OK: {ep.name} -> {ep.value}')
        break
else:
    print('NOT FOUND')
"
```

If not found, the image wasn't built correctly or the container is still running the old image.

### `KeyError: 'beyondtrust_secrets_safe'` in controller logs

The credential type exists in the database but the plugin code is missing from the container. Rebuild and redeploy the custom image.

### BeyondTrust API errors at job launch

Check the controller task container logs:

```bash
podman logs automation-controller-task 2>&1 | grep -i beyondtrust
```

Common issues:
- **HTTP 401** — invalid API key/RunAs user or OAuth client credentials
- **HTTP 403** — API user doesn't have permission to read the requested secret/folder
- **Connection refused** — network connectivity from the controller container to BeyondTrust
- **Timeout** — BeyondTrust is unreachable, or the secret path/title doesn't resolve

### Authentication mapping stops working after install

This plugin only modifies the **controller** containers. Authentication and permission mapping (OIDC, SAML, LDAP) is handled by the **gateway** container, which is never touched. If auth mappings break after installing this plugin, check:

1. Confirm the gateway container was not inadvertently restarted or recreated
2. Verify authenticator maps are still present: `curl -u admin:<pass> https://localhost/api/gateway/v1/authenticator_maps/`
3. Ensure the controller was fully online before the next OIDC login attempt (the gateway syncs user attributes with the controller post-auth)

See [docs/oidc-auth-mapping-investigation.md](docs/oidc-auth-mapping-investigation.md) for a detailed investigation of this scenario.

## References

- [AWX Credential Plugins Documentation](https://github.com/ansible/awx/blob/devel/docs/credentials/credential_plugins.md)
- [BeyondTrust Secrets Safe REST API](https://docs.beyondtrust.com/bips/v25.1/docs/secrets-safe-apis)
- [BeyondTrust Secrets Safe Ansible Lookup Plugin](https://galaxy.ansible.com/ui/repo/published/beyondtrust/secrets_safe/content/lookup/secrets_safe_lookup/)
- [AAP 2.7 Image Variables](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/install-image_variables)
