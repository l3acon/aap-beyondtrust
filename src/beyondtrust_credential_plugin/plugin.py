import collections
import logging
import threading
import time

logger = logging.getLogger(__name__)

CredentialPlugin = collections.namedtuple(
    "CredentialPlugin", ["name", "inputs", "backend"]
)

# Short-lived cache to avoid redundant API calls when multiple fields
# (e.g. username + password) are resolved from the same managed account.
_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL_SECONDS = 30

beyondtrust_inputs = {
    "fields": [
        {
            "id": "url",
            "label": "BeyondTrust URL",
            "type": "string",
            "format": "url",
            "help_text": (
                "Base URL of the BeyondInsight/Password Safe API "
                "(e.g. https://bt.example.com/BeyondTrust/api/public/v3)"
            ),
        },
        {
            "id": "api_key",
            "label": "API Key",
            "type": "string",
            "secret": True,
            "help_text": "API key registered in BeyondInsight for this application.",
        },
        {
            "id": "api_user",
            "label": "API User (RunAs)",
            "type": "string",
            "help_text": "The BeyondInsight user to authenticate as.",
        },
        {
            "id": "verify_ssl",
            "label": "Verify SSL Certificates",
            "type": "string",
            "choices": ["true", "false"],
            "default": "true",
            "help_text": "Set to false only for testing with self-signed certificates.",
        },
        {
            "id": "request_duration",
            "label": "Checkout Duration (minutes)",
            "type": "string",
            "default": "1",
            "help_text": "How long to hold the credential checkout (in minutes).",
        },
    ],
    "metadata": [
        {
            "id": "system_name",
            "label": "Managed System Name",
            "type": "string",
            "help_text": "The name of the managed system in Password Safe.",
        },
        {
            "id": "account_name",
            "label": "Managed Account Name",
            "type": "string",
            "help_text": "The name of the managed account to retrieve.",
        },
    ],
    "required": ["url", "api_key", "api_user", "system_name", "account_name"],
}


def _str_to_bool(value):
    """AWX may pass boolean fields as strings from the UI."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() not in ("false", "0", "no", "")
    return bool(value)


def _get_cached(system_name, account_name):
    """Return cached credential if still valid, else None."""
    key = (system_name, account_name)
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry["ts"]) < _CACHE_TTL_SECONDS:
            return entry["value"]
        _cache.pop(key, None)
    return None


def _set_cache(system_name, account_name, value):
    key = (system_name, account_name)
    with _cache_lock:
        _cache[key] = {"value": value, "ts": time.time()}


def beyondtrust_backend(**kwargs):
    try:
        import requests
    except ImportError as e:
        raise ValueError(
            "The requests package is not installed. "
            "Install it with: pip install requests"
        ) from e

    url = kwargs["url"].rstrip("/")
    api_key = kwargs["api_key"]
    api_user = kwargs["api_user"]
    verify_ssl = _str_to_bool(kwargs.get("verify_ssl", True))
    system_name = kwargs["system_name"]
    account_name = kwargs["account_name"]

    try:
        request_duration = int(kwargs.get("request_duration", 1))
    except (ValueError, TypeError):
        request_duration = 1

    cached = _get_cached(system_name, account_name)
    if cached is not None:
        return cached

    session = requests.Session()
    session.verify = verify_ssl
    session.headers.update(
        {
            "Content-Type": "application/json",
            "Authorization": f"PS-Auth key={api_key}; runas={api_user};",
        }
    )

    request_id = None
    try:
        # Step 1: Authenticate
        resp = session.post(f"{url}/Auth/SignAppin", timeout=30)
        resp.raise_for_status()

        # Step 2: Find managed account
        resp = session.get(
            f"{url}/ManagedAccounts",
            params={"systemName": system_name, "accountName": account_name},
            timeout=30,
        )
        resp.raise_for_status()
        accounts = resp.json()
        if not accounts:
            raise ValueError(
                f"No managed account found: system={system_name}, "
                f"account={account_name}"
            )
        account_id = accounts[0]["AccountId"]
        system_id = accounts[0]["SystemId"]

        # Step 3: Request credential checkout
        resp = session.post(
            f"{url}/Requests",
            json={
                "AccountId": account_id,
                "SystemId": system_id,
                "DurationMinutes": request_duration,
                "Reason": "AAP credential lookup",
            },
            timeout=30,
        )
        resp.raise_for_status()
        request_id = resp.json()

        # Step 4: Retrieve the credential
        resp = session.get(f"{url}/Credentials/{request_id}", timeout=30)
        resp.raise_for_status()
        credential_value = resp.text.strip('"')

        # Step 5: Check in the request
        try:
            session.put(f"{url}/Requests/{request_id}/Checkin", timeout=30)
        except Exception:
            logger.warning(
                "Failed to check in request %s — it will expire after %d minutes",
                request_id,
                request_duration,
            )

        _set_cache(system_name, account_name, credential_value)
        return credential_value

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        raise ValueError(
            f"BeyondTrust API error (HTTP {status}): {e}"
        ) from e

    except requests.exceptions.ConnectionError as e:
        raise ValueError(
            f"Cannot connect to BeyondTrust at {url}: {e}"
        ) from e

    except requests.exceptions.Timeout as e:
        raise ValueError(
            f"BeyondTrust API request timed out: {e}"
        ) from e

    finally:
        try:
            session.post(f"{url}/Auth/Signout", timeout=10)
        except Exception:
            pass


beyondtrust_plugin = CredentialPlugin(
    "BeyondTrust Password Safe Lookup",
    inputs=beyondtrust_inputs,
    backend=beyondtrust_backend,
)
