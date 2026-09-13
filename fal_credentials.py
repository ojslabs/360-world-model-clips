"""Ephemeral browser-owned Fal credentials. Never writes credentials to disk."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from http.cookies import SimpleCookie, CookieError
import json
import re
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

COOKIE_NAME = "wmc_fal_session"
TTL_SECONDS = 8 * 60 * 60
MAX_CREDENTIALS = 256
LOCK = threading.RLock()
STORE = {}


class CredentialError(ValueError):
    pass


@dataclass(frozen=True)
class Credential:
    key: str = field(repr=False)
    fingerprint: str
    expires_at: float
    verified: bool = False


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate_key(key):
    if (not isinstance(key, str) or not 8 <= len(key) <= 512
            or any(ord(char) < 33 or ord(char) > 126 for char in key)):
        raise CredentialError("Enter a valid Fal API key with 8 to 512 characters and no spaces.")
    return key


def verify_key(key):
    """Read account storage settings without creating an inference request."""
    request = Request("https://api.fal.ai/v1/storage/settings",
                      headers={"Authorization": "Key " + key, "Accept": "application/json"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            value = json.loads(response.read(65537))
    except HTTPError as error:
        if error.code == 403:
            # Inference-only keys may lack account:settings:read. Keep the
            # distinction visible; do not claim authentication was verified.
            return False
        if error.code == 401:
            raise CredentialError("Fal did not accept this key. Check it and try again.") from None
        raise CredentialError("Fal could not verify the key right now. Try again shortly.") from None
    except (URLError, TimeoutError, OSError, ValueError, UnicodeError):
        raise CredentialError("Fal could not verify the key right now. Try again shortly.") from None
    if not isinstance(value, dict):
        raise CredentialError("Fal returned an unexpected key-check response.")
    return True


def _token(cookie):
    try:
        parsed = SimpleCookie()
        parsed.load(cookie or "")
        token = parsed[COOKIE_NAME].value if COOKIE_NAME in parsed else ""
        return token if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) else None
    except (CookieError, TypeError):
        return None


def _purge():
    now = time.time()
    for token, credential in list(STORE.items()):
        if credential.expires_at <= now:
            STORE.pop(token, None)


def _expire(token, fingerprint):
    with LOCK:
        credential = STORE.get(token)
        if credential and credential.fingerprint == fingerprint:
            STORE.pop(token, None)


def set_key(cookie, key):
    key = validate_key(key)
    verified = verify_key(key)
    credential = Credential(key, hashlib.sha256(("fal-browser-v1\0" + key).encode()).hexdigest(),
                            time.time() + TTL_SECONDS, verified)
    token = secrets.token_urlsafe(32)
    with LOCK:
        _purge()
        previous = _token(cookie)
        if len(STORE) >= MAX_CREDENTIALS and previous not in STORE:
            raise CredentialError("This server has too many active key sessions. Try again later.")
        STORE.pop(previous, None)
        STORE[token] = credential
    expiry = threading.Timer(TTL_SECONDS, _expire, args=(token, credential.fingerprint))
    expiry.daemon = True
    expiry.start()
    return token, credential


def get(cookie):
    with LOCK:
        _purge()
        return STORE.get(_token(cookie))


def require(cookie):
    credential = get(cookie)
    if credential is None:
        raise CredentialError("Connect your Fal API key before generating.")
    return credential


def clear(cookie):
    with LOCK:
        STORE.pop(_token(cookie), None)


def status(cookie):
    credential = get(cookie)
    return ({"configured": True, "expires_at": credential.expires_at, "verified": credential.verified,
             "message": "Key verified without generating media." if credential.verified else
                        "Key connected; generation access is checked on the first request."}
            if credential else {"configured": False, "verified": False})


def cookie_header(token="", *, secure=False, clear=False):
    if not clear and not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise CredentialError("Invalid browser credential session.")
    return (f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={0 if clear else TTL_SECONDS}"
            + ("; Secure" if secure else ""))
