import collections
import logging
import threading
import time

logger = logging.getLogger(__name__)

CredentialPlugin = collections.namedtuple(
    "CredentialPlugin", ["name", "inputs", "backend"]
)

_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL_SECONDS = 30


def str_to_bool(value):
    """AWX may pass boolean fields as strings from the UI."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() not in ("false", "0", "no", "")
    return bool(value)


def get_cached(*key_parts):
    """Return cached credential if still valid, else None."""
    key = key_parts
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry["ts"]) < _CACHE_TTL_SECONDS:
            return entry["value"]
        _cache.pop(key, None)
    return None


def set_cache(value, *key_parts):
    """Store a credential in the short-lived cache."""
    key = key_parts
    with _cache_lock:
        _cache[key] = {"value": value, "ts": time.time()}


def create_session(url, api_key, api_user, verify_ssl, client_id=None, client_secret=None):
    """Create an authenticated requests.Session against the BeyondTrust API.

    Supports two auth modes:
    - API key + RunAs user (traditional)
    - OAuth2 client_credentials (client_id + client_secret)
    """
    try:
        import requests
    except ImportError as e:
        raise ValueError(
            "The requests package is not installed. "
            "Install it with: pip install requests"
        ) from e

    session = requests.Session()
    session.verify = verify_ssl
    session.headers["Content-Type"] = "application/json"

    if client_id and client_secret:
        token_resp = session.post(
            f"{url}/Auth/connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        token_resp.raise_for_status()
        token = token_resp.json()["access_token"]
        session.headers["Authorization"] = f"Bearer {token}"
    elif api_key:
        session.headers["Authorization"] = f"PS-Auth key={api_key}; runas={api_user};"
    else:
        raise ValueError(
            "Either api_key (with api_user) or client_id/client_secret must be provided."
        )

    return session


def sign_in(session, url):
    """Authenticate the session with BeyondTrust."""
    resp = session.post(f"{url}/Auth/SignAppin", timeout=30)
    resp.raise_for_status()
    return resp


def sign_out(session, url):
    """End the session. Errors are swallowed."""
    try:
        session.post(f"{url}/Auth/Signout", timeout=10)
    except Exception:
        pass


def handle_request_error(e, url):
    """Convert requests exceptions to ValueError with clear messages."""
    import requests

    if isinstance(e, requests.exceptions.HTTPError):
        status = e.response.status_code if e.response is not None else "unknown"
        raise ValueError(f"BeyondTrust API error (HTTP {status}): {e}") from e
    elif isinstance(e, requests.exceptions.ConnectionError):
        raise ValueError(f"Cannot connect to BeyondTrust at {url}: {e}") from e
    elif isinstance(e, requests.exceptions.Timeout):
        raise ValueError(f"BeyondTrust API request timed out: {e}") from e
    else:
        raise ValueError(f"BeyondTrust API error: {e}") from e
