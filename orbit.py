"""Run the app preset using an explicit process FAL_KEY and an in-memory session."""
from __future__ import annotations

import argparse
import json
import os
from http.cookiejar import CookieJar
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, ProxyHandler, Request, build_opener

BASE_URL = "http://127.0.0.1:8476"
WAIT_SECONDS = 20 * 60
POLL_SECONDS = 3
TERMINAL = {"complete", "failed", "interrupted"}


class OrbitError(RuntimeError):
    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def api(path, data=None, *, cookie_jar=None):
    """One local HTTP request. Mutations are never automatically repeated."""
    if not path.startswith("/api/"):
        raise OrbitError("Only the local app API can be called.")
    body = None if data is None else json.dumps(data).encode()
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Origin"] = BASE_URL
    request = Request(BASE_URL + path, data=body, headers=headers)
    try:
        handlers = [ProxyHandler({}), NoRedirect()]
        if cookie_jar is not None:
            handlers.append(HTTPCookieProcessor(cookie_jar))
        with build_opener(*handlers).open(request, timeout=20) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
    except HTTPError as error:
        try:
            payload = json.loads(error.read(4096))
            message = payload.get("error") if isinstance(payload, dict) else None
        except (ValueError, UnicodeError):
            message = None
        if error.code == 401:
            message = "This server requires browser sign-in. Use the signed-in app, or a local CLI server without shared-demo authentication."
        raise OrbitError(str(message or f"Local app returned HTTP {error.code}."), http_status=error.code) from None
    except (URLError, TimeoutError, OSError):
        raise OrbitError("Could not reach the local app at " + BASE_URL + ". No request was retried.") from None
    if len(raw) > 4 * 1024 * 1024:
        raise OrbitError("The local app returned an oversized response.")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise OrbitError("The local app returned unreadable JSON. No request was retried.") from None
    if not isinstance(value, dict):
        raise OrbitError("The local app returned an unexpected response.")
    return value


def submit_generation(video_id):
    """Use a private cookie jar for this call, then revoke its temporary key."""
    key = os.environ.get("FAL_KEY", "")
    if not key:
        raise OrbitError("Set FAL_KEY explicitly in this CLI process before running. No server key is used.")
    cookies = CookieJar()
    connected = False
    try:
        status = api("/api/credentials/fal", {"key": key}, cookie_jar=cookies)
        if not status.get("configured"):
            raise OrbitError("The local server did not connect this Fal key.")
        connected = True
        return api("/api/generate", {"video_id": video_id, "mode": "fal-h3-max"}, cookie_jar=cookies)
    except OrbitError as error:
        raise OrbitError(str(error).replace(key, "[credential]"), http_status=error.http_status) from None
    finally:
        if connected:
            try:
                api("/api/credentials/fal/clear", {}, cookie_jar=cookies)
            except (OrbitError, KeyboardInterrupt):
                print("Could not clear the temporary server key session; it will expire automatically.", file=sys.stderr)
        cookies.clear()


def _job_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise OrbitError("The local job ID is missing or invalid.")
    return value


def _video_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        raise OrbitError("Choose an imported 11-character YouTube video ID.")
    return value


def _run_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise OrbitError("The local run ID is missing or invalid.")
    return value


def _show_job(job):
    status = job.get("status")
    if status not in {"queued", "running", *TERMINAL}:
        raise OrbitError("The local job status is missing or invalid.")
    print("Status: " + status, flush=True)
    if status == "complete":
        result = job.get("result", {})
        path = result.get("url") if isinstance(result, dict) else None
        if not isinstance(path, str) or not path.startswith("/media/"):
            raise OrbitError("The completed job did not include a local output URL.")
        parsed = urlparse(path)
        if parsed.scheme or parsed.netloc:
            raise OrbitError("The completed job returned an unexpected output URL.")
        print("Output: " + BASE_URL + path, flush=True)
        return 0
    if status in {"failed", "interrupted"}:
        print(str(job.get("error") or "This orbit did not complete. No retry was submitted."), file=sys.stderr)
        return 1
    return None


def show_status(job_id, wait=False, video_id=None, run_id=None):
    job_id = _job_id(job_id)
    if (video_id is None) != (run_id is None):
        raise OrbitError("Use both --video-id and --run-id to recover a run after a server restart.")
    if run_id is not None:
        video_id, run_id = _video_id(video_id), _run_id(run_id)
    deadline = time.monotonic() + WAIT_SECONDS
    last_status = object()
    use_run = False
    while True:
        if not use_run:
            try:
                job = api("/api/jobs/" + job_id)
            except OrbitError as error:
                if error.http_status != 404 or run_id is None:
                    raise
                use_run = True
                print("Job history was unavailable. Reading the saved run; no generation was submitted.", flush=True)
        if use_run:
            job = api(f"/api/runs/{video_id}/{run_id}")
        status = job.get("status")
        if status != last_status or status in TERMINAL:
            result = _show_job(job)
            last_status = status
            if result is not None:
                return result
        if not wait:
            return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OrbitError("Stopped waiting after 20 minutes. The server job was not cancelled or resubmitted.")
        time.sleep(min(POLL_SECONDS, remaining))


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Generate the selected saved frame with the app's preset")
    run.add_argument("--video-id", help="Imported video ID; defaults to the app's seed")
    run.add_argument("--no-wait", action="store_true", help="Print the job ID and return immediately")
    status = commands.add_parser("status", help="Read an existing local job without submitting another")
    status.add_argument("job_id")
    status.add_argument("--wait", action="store_true", help="Wait up to 20 minutes for this job")
    status.add_argument("--video-id", help="Imported video ID for saved-run recovery after a restart")
    status.add_argument("--run-id", help="Saved run ID returned by the original generation request")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    job_id = None
    video_id = None
    run_id = None
    submitting = False
    try:
        if args.command == "run":
            video_id = _video_id(args.video_id or api("/api/state").get("seed"))
            submitting = True
            started = submit_generation(video_id)
            job_id = _job_id(started.get("job_id"))
            print("Job: " + job_id, flush=True)
            if started.get("run_id") is not None:
                run_id = _run_id(started["run_id"])
                print("Run: " + run_id, flush=True)
            if args.no_wait:
                return 0
            return show_status(job_id, wait=True, video_id=video_id if run_id else None, run_id=run_id)
        job_id = _job_id(args.job_id)
        video_id, run_id = args.video_id, args.run_id
        print("Job: " + job_id, flush=True)
        return show_status(job_id, wait=args.wait, video_id=video_id, run_id=run_id)
    except (OrbitError, KeyboardInterrupt) as error:
        message = "Stopped waiting. The server job was not cancelled." if isinstance(error, KeyboardInterrupt) else str(error)
        print(message, file=sys.stderr, flush=True)
        if job_id:
            resume = f"Resume: python orbit.py status {job_id} --wait"
            if video_id and run_id:
                resume += f" --video-id {video_id} --run-id {run_id}"
            print(resume, file=sys.stderr, flush=True)
        elif submitting:
            print("No local job ID was received. Check the app before running again; the request was not retried.",
                  file=sys.stderr, flush=True)
        return 130 if isinstance(error, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
