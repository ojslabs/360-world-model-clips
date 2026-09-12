"""Issue bounded credentials for one explicit live Reactor preview."""

import reactor_api
from remix_catalog import MODEL

TOKEN_SECONDS = 360
SESSION_SECONDS = 300


def token():
    try:
        result = reactor_api.mint_token(MODEL, expires_after=TOKEN_SECONDS,
                                        max_session_duration_seconds=SESSION_SECONDS)
    except reactor_api.ReactorError as error:
        raise ValueError(str(error)) from None
    return {"jwt": result["jwt"], "model": MODEL, "expires_in": TOKEN_SECONDS,
            "max_session_seconds": SESSION_SECONDS}
