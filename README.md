# AAP BeyondTrust Password Safe Credential Plugin

A custom credential plugin for Ansible Automation Platform (AAP) 2.7 that retrieves secrets from BeyondTrust Password Safe **at job launch time** on the controller — not during playbook execution on endpoints.

When a job template runs, AAP resolves linked credential fields server-side before the playbook ever reaches a managed host. This plugin calls the BeyondTrust REST API to check out a credential and returns it to AAP for injection into the job environment.

## Prerequisites

- AAP 2.7 containerized deployment on RHEL
- Podman on the controller host
- Network connectivity from the controller to your BeyondTrust Password Safe API
- A BeyondTrust API Registration configured for AAP with:
  - An API key
  - A RunAs user with permissions to request managed account credentials
  - Auto-approve policies for the API user (otherwise checkout requests will hang waiting for human approval)

## Deployment

Credential plugins run inside the **controller's Django process**, not in an Execution Environment. The plugin must be baked into a custom controller container image.

### 1. Identify your base controller image

On the AAP host, find the current controller image:

```bash
podman ps --format '{{.Names}} {{.Image}}' | grep controller
```

Update the `ARG BASE_IMAGE` in the `Containerfile` if it differs from the default.

### 2. Build the custom image

Clone this repo onto your AAP controller host and build:

```bash
git clone https://github.com/l3acon/aap-beyondtrust.git
cd aap-beyondtrust
podman build -t <your-controller-image>:latest .
```

To deploy in-place (tagging over the existing image so containers pick it up on recreate):

```bash
podman build -t $(podman inspect automation-controller-web --format '{{.ImageName}}') .
```

### 3. Recreate the controller containers

The controller runs as three containers managed by systemd user units: `automation-controller-web`, `automation-controller-task`, and `automation-controller-rsyslog`.

After building the new image, recreate the containers so they use it:

```bash
# Stop the containers
systemctl --user stop automation-controller-web automation-controller-task automation-controller-rsyslog

# Remove old containers
podman rm automation-controller-web automation-controller-task automation-controller-rsyslog

# Re-run the AAP installer to recreate them with the new image
cd ~/aap27-bundle  # or wherever your installer bundle lives
ansible-playbook -i inventory collections/ansible_collections/ansible/containerized_installer/playbooks/install.yml
```

Alternatively, if the installer is unavailable or has connectivity issues, you can restart via systemd after tagging the new image over the old one:

```bash
podman build -t $(podman inspect automation-controller-web --format '{{.ImageName}}') .
podman stop automation-controller-web automation-controller-task automation-controller-rsyslog
podman rm automation-controller-web automation-controller-task automation-controller-rsyslog
# Then re-run the installer, or recreate containers manually
```

### 4. Register the credential type

Once the controller containers are running with the new image:

```bash
podman exec automation-controller-web awx-manage setup_managed_credential_types
```

### 5. Verify

Confirm the plugin is registered:

```bash
podman exec automation-controller-web awx-manage shell -c "
from awx.main.models import CredentialType
ct = CredentialType.objects.get(name__icontains='beyondtrust')
print(f'{ct.name} (ID={ct.id}, kind={ct.kind})')
"
```

Expected output:
```
BeyondTrust Password Safe Lookup (ID=33, kind=external)
```

## Usage

### Create a BeyondTrust source credential

1. In AAP, navigate to **Resources → Credentials → Add**
2. Select credential type: **BeyondTrust Password Safe Lookup**
3. Fill in:
   - **BeyondTrust URL** — base API URL (e.g. `https://bt.example.com/BeyondTrust/api/public/v3`)
   - **API Key** — the key from your BeyondInsight API Registration
   - **API User (RunAs)** — the BeyondInsight user to authenticate as
   - **Verify SSL Certificates** — `true` for production, `false` for lab/self-signed
   - **Checkout Duration** — how long to hold the checkout (default: 1 minute)

### Link to a target credential

1. Create or edit a target credential (e.g. a **Machine** credential)
2. Click the key/link icon next to the field you want to populate (e.g. `Password`)
3. Select your BeyondTrust credential as the source
4. Provide the metadata:
   - **Managed System Name** — the system name in Password Safe
   - **Managed Account Name** — the account name to retrieve
5. Save

### Use in a Job Template

Attach the target credential (e.g. Machine credential) to your Job Template as normal. At job launch, AAP will:

1. Call the BeyondTrust API to check out the credential
2. Inject the password into the job
3. Check the credential back in

The playbook and managed hosts never see the BeyondTrust API — they just receive the resolved credential.

## Project Structure

```
aap-beyondtrust/
├── README.md
├── RESEARCH.md                                 # Design decisions and background research
├── Containerfile                               # Extends the AAP controller image
├── pyproject.toml                              # Package config + entry points
├── .dockerignore
├── .gitignore
└── src/
    └── beyondtrust_credential_plugin/
        ├── __init__.py
        └── plugin.py                           # Plugin implementation
```

## How It Works

The plugin implements the BeyondTrust Password Safe REST API credential retrieval workflow:

1. **Authenticate** — `POST /Auth/SignAppin` with API key + RunAs user
2. **Find account** — `GET /ManagedAccounts?systemName=X&accountName=Y`
3. **Request checkout** — `POST /Requests` with account ID and duration
4. **Retrieve credential** — `GET /Credentials/{requestId}`
5. **Check in** — `PUT /Requests/{requestId}/Checkin`
6. **Sign out** — `POST /Auth/Signout`

The plugin includes a 30-second in-memory cache so that multiple field lookups for the same managed account (e.g. resolving both username and password) reuse a single checkout rather than making redundant API calls.

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

### `KeyError: 'beyondtrust_password_safe'` in controller logs

The credential type exists in the database but the plugin code is missing from the container. Rebuild and redeploy the custom image.

### BeyondTrust API errors at job launch

Check the controller task container logs:

```bash
podman logs automation-controller-task 2>&1 | grep -i beyondtrust
```

Common issues:
- **HTTP 401** — invalid API key or RunAs user
- **HTTP 403** — API user doesn't have permission for the requested account
- **Connection refused** — network connectivity from the controller container to BeyondTrust
- **Timeout** — BeyondTrust is unreachable or the request is pending approval (auto-approve not configured)

## References

- [AWX Credential Plugins Documentation](https://github.com/ansible/awx/blob/devel/docs/credentials/credential_plugins.md)
- [BeyondTrust Password Safe REST API](https://docs.beyondtrust.com/bips/v25.2/docs/api)
- [AAP 2.7 Image Variables](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/install-image_variables)
