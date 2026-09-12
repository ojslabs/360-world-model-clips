"""Check local media tools without credentials, source downloads or paid requests."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def check_tools(require_model=False):
    import crowd_audio
    import media
    from yt_dlp.utils._jsruntime import NodeJsRuntime
    checks, errors = {}, []
    for package in ("numpy", "scipy", "Pillow", "onnxruntime", "yt-dlp"):
        try:
            checks[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            errors.append(f"Missing Python dependency: {package}")
    for tool in ("ffmpeg", "ffprobe", "node"):
        command = os.environ.get("FOOTBALL_FFMPEG") if tool == "ffmpeg" else None
        command = command or shutil.which(tool)
        if not command or not Path(command).is_file():
            errors.append(f"Missing {tool}; install it and put it on PATH.")
            continue
        result = subprocess.run([command, "--version" if tool == "node" else "-version"],
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            errors.append(f"{tool} could not report its version.")
        else:
            checks[tool] = result.stdout.splitlines()[0]
            if tool == "node":
                match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", checks[tool].strip())
                minimum = NodeJsRuntime.MIN_SUPPORTED_VERSION
                if not match or tuple(map(int, match.groups())) < minimum:
                    errors.append("yt-dlp requires Node.js " + ".".join(map(str, minimum)) + " or newer.")
    sibling = Path(sys.executable).parent / "yt-dlp"
    downloader = (os.environ.get("FOOTBALL_YTDLP") or (str(sibling) if sibling.is_file() else None)
                  or shutil.which("yt-dlp") or str(sibling))
    if not Path(downloader).is_file():
        errors.append("yt-dlp executable missing; activate .venv or set FOOTBALL_YTDLP.")
    else:
        checks["yt_dlp_executable"] = downloader
    if "ffmpeg" in checks:
        for args, pattern, label in ((["-decoders"], r"\blibdav1d\b", "fast AV1 decoding (libdav1d)"),
                                     (["-encoders"], r"\blibx264\b", "H.264 encoding (libx264)"),
                                     (["-filters"], r"\bzscale\b", "HDR conversion (zscale)")):
            result = media.run(["ffmpeg", "-hide_banner", *args]).decode(errors="replace")
            if not re.search(pattern, result):
                errors.append(f"FFmpeg is missing {label}.")
    if require_model:
        path = crowd_audio.MODEL_PATH
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != crowd_audio.MODEL_SHA256:
            errors.append("The pinned speech model is missing or invalid. Run python crowd_audio.py --setup-model.")
        else:
            checks["vad_model_sha256"] = crowd_audio.MODEL_SHA256
    return {"ok": not errors, "checks": checks, "errors": errors}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="store_true", help="Verify the local speech model checksum too.")
    result = check_tools(parser.parse_args().model)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
