"""Issue bounded credentials for one explicit live Reactor preview."""

import reactor_api
from remix_catalog import MODEL

TOKEN_SECONDS = 360
SESSION_SECONDS = 300


def token(*, credential_key=reactor_api.ENV_CREDENTIAL):
    try:
        result = reactor_api.mint_token(MODEL, expires_after=TOKEN_SECONDS,
                                        max_session_duration_seconds=SESSION_SECONDS,
                                        credential_key=credential_key)
    except reactor_api.ReactorError:
        raise ValueError("Reactor could not start the live connection. Check your key and try again.") from None
    return {"jwt": result["jwt"], "model": MODEL, "expires_in": TOKEN_SECONDS,
            "max_session_seconds": SESSION_SECONDS}
