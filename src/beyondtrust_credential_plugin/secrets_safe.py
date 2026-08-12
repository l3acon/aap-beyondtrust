import logging

from ._common import (
    CredentialPlugin,
    create_session,
    get_cached,
    handle_request_error,
    set_cache,
    sign_in,
    sign_out,
    str_to_bool,
)

logger = logging.getLogger(__name__)

secrets_safe_inputs = {
    "fields": [
        {
            "id": "url",
            "label": "BeyondTrust URL",
            "type": "string",
            "format": "url",
            "help_text": (
                "Base URL of the BeyondInsight/Secrets Safe API "
                "(e.g. https://bt.example.com/BeyondTrust/api/public/v3)"
            ),
        },
        {
            "id": "api_key",
            "label": "API Key",
            "type": "string",
            "secret": True,
            "help_text": (
                "API key registered in BeyondInsight. "
                "Provide RunAs user along with the key. "
                "Leave blank if using OAuth client credentials instead."
            ),
        },
        {
            "id": "api_user",
            "label": "API User (RunAs)",
            "type": "string",
            "help_text": "The BeyondInsight user to authenticate as (used with API Key).",
        },
        {
            "id": "client_id",
            "label": "OAuth Client ID",
            "type": "string",
            "help_text": "OAuth2 Client ID. Required only if API Key is not set.",
        },
        {
            "id": "client_secret",
            "label": "OAuth Client Secret",
            "type": "string",
            "secret": True,
            "help_text": "OAuth2 Client Secret. Required only if API Key is not set.",
        },
        {
            "id": "verify_ssl",
            "label": "Verify SSL Certificates",
            "type": "string",
            "choices": ["true", "false"],
            "default": "true",
            "help_text": "Set to false only for testing with self-signed certificates.",
        },
    ],
    "metadata": [
        {
            "id": "secret_path",
            "label": "Secret Path",
            "type": "string",
            "help_text": "Folder path to the secret (e.g. folder1/folder2).",
        },
        {
            "id": "secret_title",
            "label": "Secret Title",
            "type": "string",
            "help_text": "The title of the secret to retrieve.",
        },
        {
            "id": "secret_field",
            "label": "Secret Field",
            "type": "string",
            "default": "password",
            "choices": ["password", "username", "text"],
            "help_text": "Which field to return from the secret (password, username, or text).",
        },
        {
            "id": "separator",
            "label": "Path Separator",
            "type": "string",
            "default": "/",
            "help_text": "Character used to separate folder names in the path.",
        },
    ],
    "required": ["url", "secret_path", "secret_title"],
}


def secrets_safe_backend(**kwargs):
    import requests

    url = kwargs["url"].rstrip("/")
    api_key = kwargs.get("api_key", "")
    api_user = kwargs.get("api_user", "")
    client_id = kwargs.get("client_id", "")
    client_secret = kwargs.get("client_secret", "")
    verify_ssl = str_to_bool(kwargs.get("verify_ssl", True))

    secret_path = kwargs["secret_path"]
    secret_title = kwargs["secret_title"]
    secret_field = kwargs.get("secret_field", "password").lower()
    separator = kwargs.get("separator", "/")

    cached = get_cached("ss", secret_path, secret_title, secret_field)
    if cached is not None:
        return cached

    session = create_session(
        url, api_key, api_user, verify_ssl,
        client_id=client_id, client_secret=client_secret,
    )

    try:
        sign_in(session, url)

        resp = session.get(
            f"{url}/Secrets-Safe/Secrets",
            params={
                "Path": secret_path,
                "Title": secret_title,
                "Separator": separator,
                "Decrypt": "true",
            },
            timeout=30,
        )
        resp.raise_for_status()
        secrets = resp.json()

        if not secrets:
            raise ValueError(
                f"No secret found at path={secret_path}, title={secret_title}"
            )

        secret = secrets[0]

        field_map = {
            "password": "Password",
            "username": "Username",
            "text": "Text",
        }
        api_field = field_map.get(secret_field)
        if not api_field:
            raise ValueError(
                f"Invalid secret_field '{secret_field}'. "
                f"Must be one of: password, username, text"
            )

        value = secret.get(api_field)
        if value is None:
            available = [k for k in ("Password", "Username", "Text") if secret.get(k)]
            raise ValueError(
                f"Secret '{secret_title}' does not have field '{api_field}'. "
                f"Available fields: {available}"
            )

        set_cache(value, "ss", secret_path, secret_title, secret_field)
        return value

    except (
        requests.exceptions.HTTPError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    ) as e:
        handle_request_error(e, url)

    finally:
        sign_out(session, url)


secrets_safe_plugin = CredentialPlugin(
    "BeyondTrust Secrets Safe Lookup",
    inputs=secrets_safe_inputs,
    backend=secrets_safe_backend,
)
