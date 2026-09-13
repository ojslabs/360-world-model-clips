"""Start the built image with fresh data and verify its protected HTTP boundary."""
import argparse
import http.client
import json
import socket
import subprocess
import time
from urllib.parse import urlencode
import uuid


def docker(*arguments):
    return subprocess.run(["docker", *arguments], capture_output=True, text=True,
                          check=True, timeout=90).stdout.strip()


def verify_http(port, password):
    host = f"localhost:{port}"
    origin = f"http://{host}"

    def request(method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(method, path, body=body, headers={"Host": host, **(headers or {})})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    deadline = time.monotonic() + 60
    while True:
        try:
            status, _, body = request("GET", "/healthz")
            if status == 200 and json.loads(body) == {"ok": True}:
                break
        except (OSError, ValueError, http.client.HTTPException):
            pass
        if time.monotonic() > deadline:
            raise AssertionError("The container did not become healthy within 60 seconds.")
        time.sleep(.5)

    status, _, body = request("GET", "/api/state")
    assert status == 401, f"Unauthenticated state returned {status}, expected 401."
    assert json.loads(body)["login_url"] == "/login"
    form = urlencode({"password": password, "next": "/"})
    status, headers, _ = request("POST", "/login", form,
                                 {"Origin": origin, "Content-Type": "application/x-www-form-urlencoded"})
    assert status == 303, f"Sign-in returned {status}, expected 303."
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    assert "HttpOnly" in headers["Set-Cookie"]
    status, _, body = request("GET", "/api/state", headers={"Cookie": cookie})
    assert status == 200, f"Authenticated state returned {status}, expected 200."
    state = json.loads(body)
    assert state["projects"] == [] and state["jobs"] == [], "Fresh data must start empty."
    assert not state["generation"].get("configured"), "Container smoke must not have a Fal key."
    assert not state["fal_credentials"]["configured"] and not state["reactor_credentials"]["configured"], "Both provider connections must start empty."
    status, _, body = request("GET", "/", headers={"Cookie": cookie})
    assert status == 200 and b'football-edits-build' in body, "The built editor must load after sign-in."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="world-model-clips:ci")
    args = parser.parse_args()
    identity = "clips-smoke-" + uuid.uuid4().hex[:12]
    volume = identity + "-data"
    password = "ci-only-disposable-passcode-" + uuid.uuid4().hex
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    try:
        docker("volume", "create", volume)
        docker("run", "--detach", "--name", identity,
               "--publish", f"127.0.0.1:{port}:8476", "--mount", f"source={volume},target=/data",
               "--env", "FAL_KEY=", "--env", f"DEMO_PASSWORD={password}",
               "--env", f"PUBLIC_ORIGIN=http://localhost:{port}", args.image)
        assert docker("exec", identity, "id", "-u") != "0", "The image must run as a non-root user."
        verify_http(port, password)
        docker("exec", identity, "python", "-c",
               "from pathlib import Path; p=Path('/data/.write-check'); p.write_text('ok'); p.unlink()")
        print("Container smoke passed: non-root startup, writable fresh data, health, API gate, sign-in and empty editor.")
    except Exception:
        subprocess.run(["docker", "logs", identity], check=False)
        raise
    finally:
        subprocess.run(["docker", "rm", "--force", identity], capture_output=True, check=False)
        subprocess.run(["docker", "volume", "rm", "--force", volume], capture_output=True, check=False)


if __name__ == "__main__":
    main()
