"""One protected shared demo workspace, with no user accounts or per-user storage."""
from __future__ import annotations

import base64
from collections import deque
import hashlib
import hmac
import html
from http.cookies import CookieError, SimpleCookie
import ipaddress
import json
import os
import secrets
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit

COOKIE = "world_model_demo"
SESSION_SECONDS = 24 * 60 * 60
FAILURE_LIMIT = 10
FAILURE_SECONDS = 60
FAILURES = {}
LOCK = threading.Lock()


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _origin():
    value = os.environ.get("PUBLIC_ORIGIN", "")
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path or parsed.port == 0 or value != f"{parsed.scheme}://{parsed.netloc}"
                or any(character.isspace() or ord(character) < 32 for character in value)):
            raise ValueError()
        if parsed.scheme != "https" and not _loopback(parsed.hostname):
            raise ValueError()
    except ValueError:
        raise ValueError("PUBLIC_ORIGIN must be a canonical HTTPS origin, without a path; HTTP is allowed only for localhost.") from None
    return parsed


def validate_deployment(host):
    """Fail before listening publicly unless the shared-demo gate is configured."""
    origin = _origin()
    password = os.environ.get("DEMO_PASSWORD", "")
    session_secret = os.environ.get("DEMO_SESSION_SECRET", "")
    if not _loopback(host) and (not password or origin is None):
        raise ValueError("An external bind requires DEMO_PASSWORD and PUBLIC_ORIGIN.")
    if password and len(password) < 16:
        raise ValueError("DEMO_PASSWORD must contain at least 16 characters.")
    if session_secret and len(session_secret) < 32:
        raise ValueError("DEMO_SESSION_SECRET must contain at least 32 characters when set.")
    if origin is not None and not _loopback(origin.hostname) and not password:
        raise ValueError("A public origin requires DEMO_PASSWORD.")


def trusted(handler):
    """Validate actual Host/Origin headers, never forwarded identity or scheme."""
    try:
        origin = _origin()
        host = handler.headers.get("Host", "")
        request_origin = handler.headers.get("Origin")
        local_hosts = {f"127.0.0.1:{handler.server.server_port}", f"localhost:{handler.server.server_port}",
                       f"[::1]:{handler.server.server_port}"}
        method = getattr(handler, "command", "GET")
        path = urlsplit(handler.path).path
        if origin is not None:
            if host != origin.netloc:
                return host in local_hosts and path == "/healthz" and method in {"GET", "HEAD"}
            expected_origin = f"{origin.scheme}://{origin.netloc}"
        else:
            if host not in local_hosts:
                return False
            expected_origin = "http://" + host
        if request_origin is not None and request_origin != expected_origin:
            return False
        # Local CLI requests keep working without authentication. Protected browser
        # mutations require an Origin even if a session cookie is valid.
        return not (method == "POST" and os.environ.get("DEMO_PASSWORD") and request_origin != expected_origin)
    except (ValueError, AttributeError):
        return False


def _safe_next(value):
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 2048:
        return "/"
    decoded = unquote(value)
    if (decoded.startswith("//") or "\\" in decoded
            or any(ord(character) < 32 or ord(character) == 127 for character in decoded)):
        return "/"
    parsed = urlsplit(value)
    return value if not parsed.scheme and not parsed.netloc else "/"


def _key():
    password = os.environ.get("DEMO_PASSWORD", "")
    secret = os.environ.get("DEMO_SESSION_SECRET", "")
    return hashlib.sha256(("world-model-demo-session-v1\0" + secret + "\0" + password).encode()).digest()


def _token():
    issued = int(time.time())
    raw = json.dumps({"iat": issued, "exp": issued + SESSION_SECONDS,
                      "nonce": secrets.token_urlsafe(16)}, separators=(",", ":")).encode()
    payload = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return payload + "." + hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()


def _authenticated(handler):
    try:
        header = handler.headers.get("Cookie", "")
        if len(header) > 8192:
            return False
        cookies = SimpleCookie()
        cookies.load(header)
        token = cookies[COOKIE].value
        if len(token) > 512:
            return False
        payload, signature = token.split(".")
        expected = hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        value = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        issued, expires = value["iat"], value["exp"]
        now = time.time()
        return (type(issued) is int and type(expires) is int and expires - issued == SESSION_SECONDS
                and issued <= now + 60 and now < expires)
    except (CookieError, KeyError, ValueError, TypeError, UnicodeError):
        return False


def _cookie(value, *, clear=False):
    origin = _origin()
    secure = origin is not None and origin.scheme == "https"
    return (f"{COOKIE}={value}; Path=/; Max-Age={0 if clear else SESSION_SECONDS}; HttpOnly; SameSite=Strict"
            + ("; Secure" if secure else ""))


def _send(handler, status, body=b"", *, content_type="text/plain; charset=utf-8", headers=(), head=False):
    if isinstance(body, str):
        body = body.encode()
    handler.send_response(status)
    for name, value in (("Content-Type", content_type), ("Content-Length", str(len(body))),
                        ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                        ("X-Frame-Options", "DENY"), ("Connection", "close"), *headers):
        handler.send_header(name, value)
    handler.end_headers()
    if not head and getattr(handler, "command", "GET") != "HEAD":
        handler.wfile.write(body)
    handler.close_connection = True
    return True


def _redirect(handler, location, *, cookie=None, head=False):
    headers = [("Location", location)]
    if cookie is not None:
        headers.append(("Set-Cookie", cookie))
    return _send(handler, 303, headers=headers, head=head)


def _allow_attempt(address, *, clear=False):
    now = time.monotonic()
    with LOCK:
        for key in list(FAILURES):
            values = FAILURES[key]
            while values and values[0] <= now - FAILURE_SECONDS:
                values.popleft()
            if not values:
                del FAILURES[key]
        if clear:
            FAILURES.pop(address, None)
            return True
        # Reserve the slot atomically before reading the form, so concurrent failed
        # attempts cannot all pass a separate check. Success clears this peer's count.
        if len(FAILURES.get(address, ())) >= FAILURE_LIMIT:
            return False
        if address not in FAILURES and len(FAILURES) >= 4096:
            return False
        FAILURES.setdefault(address, deque()).append(now)
        return True


def _login_page(next_path):
    return ("<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>Sign in to World Model Clips</title><style>body{background:#101010;color:#eee;font:18px system-ui;"
            "max-width:420px;margin:15vh auto;padding:24px}input,button{box-sizing:border-box;width:100%;padding:12px;"
            "margin:8px 0 20px;font:inherit}button{cursor:pointer}p{line-height:1.5;color:#aaa}</style>"
            "<h1>World Model Clips</h1><p>Enter the shared demo passcode. People with access use the same workspace.</p>"
            "<form method=post action=/login><label for=password>Passcode</label>"
            "<input id=password name=password type=password autocomplete=current-password required maxlength=1024>"
            f'<input type=hidden name=next value="{html.escape(next_path, quote=True)}">'
            "<button type=submit>Sign in</button></form></html>")


def intercept(handler, head=False):
    """Handle authentication routes; return False only for authorized app traffic."""
    method = getattr(handler, "command", "HEAD" if head else "GET")
    path = urlsplit(handler.path).path
    if path == "/healthz" and method in {"GET", "HEAD"}:
        return _send(handler, 200, b'{"ok":true}', content_type="application/json", head=head)
    password = os.environ.get("DEMO_PASSWORD", "")
    if not password:
        return False
    if path == "/login" and method in {"GET", "HEAD"}:
        try:
            query = parse_qs(urlsplit(handler.path).query, max_num_fields=8)
            next_path = _safe_next(query.get("next", ["/"])[0])
        except ValueError:
            next_path = "/"
        return _send(handler, 200, _login_page(next_path), content_type="text/html; charset=utf-8", head=head,
                     headers=[("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")])
    if path == "/login" and method == "POST":
        address = handler.client_address[0]
        if not _allow_attempt(address):
            return _send(handler, 429, "Too many attempts. Try again in one minute.", headers=[("Retry-After", "60")])
        try:
            length = int(handler.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096 or handler.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded":
                raise ValueError()
            values = parse_qs(handler.rfile.read(length).decode("utf-8"), keep_blank_values=True,
                              strict_parsing=True, max_num_fields=4)
            submitted = values.get("password", [])
            if len(submitted) != 1 or not hmac.compare_digest(submitted[0].encode(), password.encode()):
                return _send(handler, 401, "Incorrect passcode. Return to sign in and try again.")
            next_path = _safe_next(values.get("next", ["/"])[0])
        except (ValueError, UnicodeError):
            return _send(handler, 400, "Send a small sign-in form.")
        _allow_attempt(address, clear=True)
        return _redirect(handler, next_path, cookie=_cookie(_token()))
    if path == "/logout" and method == "POST":
        return _redirect(handler, "/login", cookie=_cookie("", clear=True))
    if _authenticated(handler):
        return False
    if path.startswith("/api/"):
        return _send(handler, 401, b'{"error":"Sign in to the shared demo.","login_url":"/login"}',
                     content_type="application/json", head=head)
    return _redirect(handler, "/login?next=" + quote(_safe_next(handler.path), safe=""), head=head)
