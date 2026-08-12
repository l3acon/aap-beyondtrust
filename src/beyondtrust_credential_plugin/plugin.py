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


def beyondtrust_backend(**kwargs):
    import requests

    url = kwargs["url"].rstrip("/")
    api_key = kwargs["api_key"]
    api_user = kwargs["api_user"]
    verify_ssl = str_to_bool(kwargs.get("verify_ssl", True))
    system_name = kwargs["system_name"]
    account_name = kwargs["account_name"]

    try:
        request_duration = int(kwargs.get("request_duration", 1))
    except (ValueError, TypeError):
        request_duration = 1

    cached = get_cached("ps", system_name, account_name)
    if cached is not None:
        return cached

    session = create_session(url, api_key, api_user, verify_ssl)

    try:
        sign_in(session, url)

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

        resp = session.get(f"{url}/Credentials/{request_id}", timeout=30)
        resp.raise_for_status()
        credential_value = resp.text.strip('"')

        try:
            session.put(f"{url}/Requests/{request_id}/Checkin", timeout=30)
        except Exception:
            logger.warning(
                "Failed to check in request %s — it will expire after %d minutes",
                request_id,
                request_duration,
            )

        set_cache(credential_value, "ps", system_name, account_name)
        return credential_value

    except (
        requests.exceptions.HTTPError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    ) as e:
        handle_request_error(e, url)

    finally:
        sign_out(session, url)


beyondtrust_plugin = CredentialPlugin(
    "BeyondTrust Password Safe Lookup",
    inputs=beyondtrust_inputs,
    backend=beyondtrust_backend,
)
