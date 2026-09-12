"""Server-only Reactor authentication and model discovery."""
from __future__ import annotations

import json
import importlib.util
import os
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

import media
from runtime_paths import data_dir

ROOT = Path(__file__).resolve().parent
BASE_URL = "https://api.reactor.inc"
FAST_H3 = "reactor/fast-h3"
LOCK = threading.RLock()


class ReactorError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # Credentials only go to the configured official API origin.
        return None


def api_key():
    value = os.environ.get("REACTOR_API_KEY", "").strip()
    if not value:
        path = ROOT / ".env.local"
        if path.exists():
            for line in path.read_text().splitlines():
                key, separator, candidate = line.partition("=")
                if separator and key.strip() == "REACTOR_API_KEY":
                    value = candidate.strip().strip("\"'")
                    break
    if not value:
        raise ReactorError("Add REACTOR_API_KEY to the server's .env.local file.")
    if not value.startswith("rk_") or any(c.isspace() for c in value):
        raise ReactorError("The Reactor API key has an invalid format.")
    return value


def request(path, data=None, key=None):
    headers = {"Accept": "application/json", "User-Agent": "Football-Edits/1.0"}
    if key:
        headers["Reactor-API-Key"] = key
    payload = None
    if data is not None:
        payload = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = Request(BASE_URL + path, data=payload, headers=headers)
    try:
        with build_opener(NoRedirect()).open(req, timeout=30) as response:
            value = json.load(response)
    except HTTPError as error:
        # Never return raw upstream bodies, request headers, keys or JWTs to the UI.
        if error.code in (401, 403):
            raise ReactorError("Reactor rejected the key or its access to this model.") from None
        raise ReactorError(f"Reactor returned HTTP {error.code}. Try checking the connection again.") from None
    except (URLError, TimeoutError, OSError):
        raise ReactorError("Could not reach Reactor. Check the connection and try again.") from None
    except (ValueError, UnicodeError):
        raise ReactorError("Reactor returned an unreadable response.") from None
    if not isinstance(value, dict):
        raise ReactorError("Reactor returned an unexpected response.")
    return value


def mint_token(model=FAST_H3, session_id=None, *, expires_after=900, max_session_duration_seconds=600):
    resources = {"models": {"match": [model]}}
    if session_id:
        resources["sessions"] = {"bind": [session_id]}
    value = request("/tokens", {
        "expires_after": expires_after,
        "authorization_details": [{
            "type": "session", "resources": resources,
            "constraints": {"max_sessions": 1, "max_session_duration_seconds": max_session_duration_seconds},
        }],
    }, key=api_key())
    if not isinstance(value.get("jwt"), str) or not value["jwt"]:
        raise ReactorError("Reactor did not return a session token.")
    return value


def status():
    try:
        api_key()
        configured = True
    except ReactorError:
        configured = False
    saved = media.read_json(data_dir(ROOT) / "reactor-status.json", {})
    value = {"provider": "Reactor", "configured": configured,
             "authenticated": False, "ready": False, "model": None,
             "catalog": [], "checked_at": None, "fast_h3_access": False,
             "exact_camera_controls": False}
    value.update(saved)
    value["configured"] = configured
    value["ready"] = bool(configured and value["fast_h3_access"]
                          and importlib.util.find_spec("fast_h3")
                          and importlib.util.find_spec("reactor_sdk"))
    value["model"] = FAST_H3 if value["ready"] else None
    if not configured:
        value["authenticated"] = False
        value["fast_h3_access"] = False
        value["message"] = "Reactor needs its server API key."
    elif value["authenticated"]:
        value["message"] = (
            "Reactor authentication verified. FastH3 access verified. "
            "The orbit test uses a prompt and your frozen frame. Exact H3 Max camera controls are not connected."
        )
    else:
        value.setdefault("message", "Reactor key is configured. Check the connection to verify access.")
    return value


def check_connection():
    with LOCK:
        result = {"authenticated": False, "fast_h3_access": False, "checked_at": int(time.time())}
        try:
            mint_token()  # Authentication and visibility check only, never creates a model session.
            result.update(authenticated=True, fast_h3_access=True)
            catalog = request("/pricing")
            result["catalog"] = [{"name": m["name"], "rate": m.get("rate", {})}
                                 for m in catalog.get("models", []) if isinstance(m.get("name"), str)]
        except ReactorError as error:
            result["message"] = str(error)
        media.write_json(data_dir(ROOT) / "reactor-status.json", result)
    return status()
