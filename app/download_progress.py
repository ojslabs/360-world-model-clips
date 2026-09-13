"""Stream bounded, structured yt-dlp progress without logging media URLs."""
from __future__ import annotations

from collections import deque
import json
import math
import os
import re
import selectors
import signal
import subprocess
import time

PREFIX = "__CLIP_DOWNLOAD__"
POST_PREFIX = "__CLIP_POSTPROCESS__"
DOWNLOAD_TEMPLATE = "download:" + PREFIX + json.dumps({
    key: f"%({field})j" for key, field in {
        "status": "progress.status", "downloaded_bytes": "progress.downloaded_bytes",
        "total_bytes": "progress.total_bytes", "total_bytes_estimate": "progress.total_bytes_estimate",
        "speed_bytes_per_second": "progress.speed", "eta_seconds": "progress.eta",
        "stream_id": "info.format_id",
    }.items()
}).replace('"%(', '%(').replace(')j"', ')j')
POST_TEMPLATE = "postprocess:" + POST_PREFIX + "%(progress.status)j"


def _number(value):
    return value if (isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value) and value >= 0) else None


def parse_progress(line):
    if line.startswith(POST_PREFIX):
        return {"stage": "merging"}
    if not line.startswith(PREFIX):
        return None
    try:
        raw = json.loads(line[len(PREFIX):])
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    total = _number(raw.get("total_bytes"))
    estimated = total is None
    if estimated:
        total = _number(raw.get("total_bytes_estimate"))
    downloaded = _number(raw.get("downloaded_bytes"))
    percent = min(100, downloaded / total * 100) if downloaded is not None and total else None
    return {"stage": "downloading", "scope": "current_stream",
            "stream_id": str(raw.get("stream_id", "unknown")), "percent": percent,
            "downloaded_bytes": downloaded, "total_bytes": total,
            "total_bytes_estimated": estimated and total is not None,
            "speed_bytes_per_second": _number(raw.get("speed_bytes_per_second")),
            "eta_seconds": _number(raw.get("eta_seconds"))}


def run(command, *, on_progress=None, timeout=7200):
    """Report each media stream separately, then merging; never retry a download."""
    command = [str(part) for part in command]
    command[1:1] = ["--newline", "--progress", "--progress-delta", "0.5", "--output-na-placeholder", "null",
                    "--progress-template", DOWNLOAD_TEMPLATE,
                    "--progress-template", POST_TEMPLATE]
    started = time.monotonic()
    recent = deque(maxlen=30)
    current = {"stage": "fetching_metadata"}
    last_report = 0

    def report(value=None):
        nonlocal current, last_report
        if value is not None:
            current = value
        last_report = time.monotonic()
        if on_progress:
            on_progress({**current, "elapsed_seconds": round(last_report - started, 3)})

    def consume(line):
        parsed = parse_progress(line)
        if parsed is not None:
            report(parsed)
        elif not line.startswith((PREFIX, POST_PREFIX)):
            recent.append(re.sub(r"https?://\S+", "[remote URL]", line[-1800:]))

    report()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            pending = b""
            while selector.get_map():
                if time.monotonic() - started > timeout:
                    raise RuntimeError("The source download timed out; its partial files were retained.")
                for key, _ in selector.select(timeout=.25):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        break
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        consume(line.decode(errors="replace").strip())
                    # No unbounded output accumulation from a malformed child.
                    pending = pending[-65536:]
                if time.monotonic() - last_report >= 1:
                    report()
            if pending:
                consume(pending.decode(errors="replace").strip())
        code = process.wait(timeout=max(.1, timeout - (time.monotonic() - started)))
        if code:
            raise RuntimeError("\n".join(recent)[-1800:] or f"yt-dlp exited {code}")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        process.stdout.close()
