"""One durable Fal H3 Max camera-controls request, using the official queue API.

Contract: https://fal.ai/models/minimax/h3-max/camera-controls/api
Queue: https://fal.ai/docs/documentation/model-apis/inference/queue
The model documents Base64 data URI inputs. No upload SDK is needed.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image

import media
import orbit_paths
from runtime_paths import data_dir

ROOT = Path(__file__).resolve().parent
MODEL = media.ORBIT_PRESET["model"]
QUEUE = "https://queue.fal.run"
TIMEOUT_SECONDS = 900
POLL_SECONDS = 3
ACTIVE_POLL_SECONDS = 1
MAX_VIDEO_BYTES = 512 * 1024 * 1024
DOWNLOAD_PROGRESS_SECONDS = .35
LOCK = threading.RLock()
ENV_CREDENTIAL = object()


class FalError(RuntimeError):
    pass


class FalResolutionError(FalError):
    """Safe public explanation for a decoded result below the requested resolution."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def api_key(credential_key=ENV_CREDENTIAL):
    if credential_key is not ENV_CREDENTIAL:
        if (not isinstance(credential_key, str) or not credential_key
                or any(c.isspace() for c in credential_key)):
            raise FalError("Connect your Fal API key before generating.")
        return credential_key
    value = os.environ.get("FAL_KEY", "").strip()
    if not value:
        path = ROOT / ".env.local"
        if path.exists():
            for line in path.read_text().splitlines():
                name, separator, candidate = line.partition("=")
                if separator and name.strip() == "FAL_KEY":
                    value = candidate.strip().strip("\"'")
                    break
    if not value:
        raise FalError("Add FAL_KEY to the server's .env.local file to use H3 Max camera controls.")
    if any(c.isspace() for c in value):
        raise FalError("The server's Fal key has an invalid format.")
    return value


def status(credential_key=ENV_CREDENTIAL):
    configured = False
    verified = False
    checked_at = None
    try:
        key = api_key(credential_key)
        configured = True
        saved = media.read_json(data_dir(ROOT) / "fal-status.json", {})
        if saved.get("key_fingerprint") == hashlib.sha256(key.encode()).hexdigest():
            verified = saved.get("authenticated") is True
            checked_at = saved.get("checked_at")
    except FalError:
        configured = False
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {"provider": "Fal", "model": MODEL, "configured": configured, "ready": configured,
            "exact_camera_controls": True, "authenticated": verified, "checked_at": checked_at,
            "message": "Fal H3 Max camera controls completed a verified local video." if verified else
            "Fal key configured; generation access has not been tested." if configured
            else "Add FAL_KEY to the server's .env.local file to use H3 Max camera controls."}


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FalError("Fal is still pending or the download timed out. The saved request was not resubmitted.")
    return remaining


def _queue_url(value, request_id, operation):
    suffix = "/status" if operation == "status" else ""
    fallback = f"{QUEUE}/{MODEL}/requests/{request_id}{suffix}"
    if value is None:
        return fallback
    if not isinstance(value, str):
        raise FalError("Fal returned an invalid queue location. The request was not resubmitted.")
    parsed = urlparse(value)
    roots = {f"/{MODEL}/requests/{request_id}", f"/minimax/h3-max/requests/{request_id}"}
    paths = {root + suffix for root in roots}
    if operation == "result":
        paths |= {root + "/response" for root in roots}
    if (parsed.scheme != "https" or parsed.netloc != "queue.fal.run" or parsed.path not in paths
            or parsed.params or parsed.query or parsed.fragment):
        raise FalError("Fal returned an invalid queue location. The request was not resubmitted.")
    return value


def _json_request(url, key, deadline, payload=None):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "queue.fal.run":
        raise FalError("Fal authentication can only be sent to its official queue.")
    headers = {"Authorization": "Key " + key, "Accept": "application/json",
               "User-Agent": "Football-Edits/1.0"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers.update({"Content-Type": "application/json", "X-Fal-No-Retry": "1",
                        "X-Fal-Request-Timeout": "600"})
    try:
        with build_opener(NoRedirect()).open(Request(url, data=body, headers=headers),
                                              timeout=min(30, _remaining(deadline))) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise FalError("Fal returned an oversized queue response.")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise FalError("Fal returned an unexpected queue response.")
        return result
    except HTTPError as error:
        detail = ""
        try:
            value = json.loads(error.read(16384))
            message = value.get("detail") if isinstance(value, dict) else None
            if isinstance(message, str):
                detail = re.sub(r"https?://\S+|rk_[A-Za-z0-9]+|eyJ[A-Za-z0-9_.-]+", "[redacted]", message.replace(key, "[credential]"))[:400]
        except (ValueError, UnicodeError, OSError):
            pass
        if error.code in {401, 403}:
            raise FalError(f"Fal rejected this request (HTTP {error.code}). {detail} No retry was submitted.") from None
        raise FalError(f"Fal returned HTTP {error.code}. No retry was submitted.") from None
    except (URLError, TimeoutError, OSError):
        raise FalError("Could not complete the Fal request. Its submission may have reached Fal; no retry was submitted.") from None
    except (ValueError, UnicodeError):
        raise FalError("Fal returned an unreadable queue response. No retry was submitted.") from None


def _download(url, destination, deadline, on_progress=None):
    if not isinstance(url, str):
        raise FalError("Fal did not return a video download location.")
    parsed = urlparse(url)
    host = parsed.hostname or ""
    allowed = host == "fal.media" or host.endswith(".fal.media")
    allowed |= host == "storage.googleapis.com" and parsed.path.startswith("/falserverless/")
    try:
        valid_port = parsed.port in {None, 443}
    except ValueError:
        valid_port = False
    if (parsed.scheme != "https" or not allowed or not valid_port or parsed.username
            or parsed.password or parsed.fragment):
        raise FalError("Fal returned an unsupported video download location.")
    temporary = destination.with_suffix(".mp4.part")
    started = time.monotonic()
    last_report = None
    downloaded = 0
    expected = None

    def report(*, complete=False, force=False):
        nonlocal last_report
        now = time.monotonic()
        if not force and last_report is not None and now - last_report < DOWNLOAD_PROGRESS_SECONDS:
            return
        last_report = now
        elapsed = max(0, now - started)
        speed = downloaded / elapsed if elapsed > 0 else None
        progress = {"stage": "downloading", "downloaded_bytes": downloaded,
                    "total_bytes": expected, "total_bytes_estimated": False,
                    "percent": round(min(100, downloaded / expected * 100), 2) if expected else None,
                    "speed_bytes_per_second": round(speed, 2) if speed is not None else None,
                    "eta_seconds": round(max(0, expected - downloaded) / speed, 2) if expected and speed else None,
                    "elapsed_seconds": round(elapsed, 3), "complete": complete}
        if on_progress is not None:
            try:
                on_progress(progress)
            except Exception:
                # Progress observers cannot discard a successfully downloaded paid result.
                pass

    try:
        # The CDN gets no Fal key, session token, cookies or queue headers.
        request = Request(url, headers={"Accept": "video/mp4", "User-Agent": "Football-Edits/1.0"})
        with build_opener(NoRedirect()).open(request, timeout=min(30, _remaining(deadline))) as response:
            content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
            if content_type not in {"video/mp4", "application/octet-stream", "binary/octet-stream"}:
                raise FalError("Fal's download did not identify itself as a video file.")
            length = response.headers.get("Content-Length")
            if length and (not length.isdigit() or int(length) > MAX_VIDEO_BYTES):
                raise FalError("Fal's video exceeds the local download limit.")
            expected = int(length) if length else None
            report(force=True)
            with temporary.open("wb") as stream:
                while True:
                    _remaining(deadline)
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    _remaining(deadline)
                    downloaded += len(chunk)
                    if downloaded > MAX_VIDEO_BYTES:
                        raise FalError("Fal's video exceeds the local download limit.")
                    stream.write(chunk)
                    report()
        if not downloaded:
            raise FalError("Fal returned an empty video file.")
        if expected is not None and downloaded != expected:
            raise FalError("Fal's video download ended before its declared size was received. The saved request was not resubmitted.")
        temporary.replace(destination)
        report(complete=True, force=True)
    except (HTTPError, URLError, TimeoutError, OSError):
        raise FalError("Could not download the completed Fal video. The saved request was not resubmitted.") from None
    finally:
        temporary.unlink(missing_ok=True)


def _verify_video(path, deadline, native=False):
    try:
        raw = json.loads(media.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                                    "-of", "json", path], timeout=min(30, _remaining(deadline))))
        video = next(s for s in raw["streams"] if s["codec_type"] == "video")
        audio = next((s for s in raw["streams"] if s["codec_type"] == "audio"), None)
        duration = float(raw["format"]["duration"])
        video_duration = float(video.get("duration", duration))
        numerator, denominator = map(int, video["avg_frame_rate"].split("/"))
        fps = numerator / denominator
        width, height = int(video["width"]), int(video["height"])
        if (not math.isfinite(duration) or not 4.5 <= duration <= 16 or not 1 <= fps <= 240
                or not math.isfinite(video_duration) or not 4.5 <= video_duration <= 16
                or width <= 0 or height <= 0 or abs(width / height - 16 / 9) > (.08 if native else .0001)
                or "mp4" not in raw["format"]["format_name"].split(",")):
            raise ValueError("Invalid video format")
        media.run(["ffmpeg", "-v", "error", "-xerror", "-i", path, "-map", "0:v:0",
                   "-f", "null", "-"], timeout=min(60, _remaining(deadline)))
        return {"duration": duration, "video_duration": video_duration,
                "width": width, "height": height, "fps": fps,
                "video_codec": video["codec_name"], "audio_codec": audio["codec_name"] if audio else None,
                "bytes": path.stat().st_size, "decoded_video": True}
    except FalError:
        raise
    except Exception:
        raise FalError("The Fal video did not pass the local format and full decode check.") from None


def _persist(path, receipt):
    media.write_json(path, receipt)
    # Persist the acknowledged request ID before another network operation.
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _numeric_timings(value):
    """Accept provider duration measurements, never diagnostics or credentials."""
    if not isinstance(value, dict):
        return {}
    return {name: number for name, number in list(value.items())[:64]
            if isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
            and not re.search(r"key|token|secret|auth|url|^eyj|^rk_", name)
            and not isinstance(number, bool) and isinstance(number, (int, float))
            and 0 <= number <= 86400 * 30 and math.isfinite(number)}


def _timing_details(receipt):
    return {"stage_started_at": receipt.get("stage_started_at"),
            "stage_timings": _numeric_timings(receipt.get("stage_timings")),
            "provider_timings": _numeric_timings(receipt.get("provider_timings")),
            "download_progress": _download_details(receipt.get("download_progress"))}


def _download_details(value):
    if not isinstance(value, dict):
        return {}
    result = {"stage": "downloading", "complete": value.get("complete") is True,
              "total_bytes_estimated": False}
    for name in ("downloaded_bytes", "total_bytes", "percent", "speed_bytes_per_second",
                 "eta_seconds", "elapsed_seconds"):
        number = value.get(name)
        result[name] = number if (isinstance(number, (int, float)) and not isinstance(number, bool)
                                 and math.isfinite(number) and number >= 0) else None
    return result


def _notify_progress(receipt, on_progress):
    if on_progress is not None:
        try:
            on_progress(receipt["stage"], _timing_details(receipt))
        except Exception:
            # A disconnected observer cannot turn a paid request into a failure.
            pass


def _stage(path, receipt, stage, on_progress=None):
    """Persist observed wall-clock stages across restarts, without resetting polls."""
    now = time.time()
    previous = receipt.get("stage")
    started = receipt.get("stage_started_at")
    if previous != stage or not isinstance(started, (int, float)) or not math.isfinite(started):
        timings = _numeric_timings(receipt.get("stage_timings"))
        if isinstance(started, (int, float)) and math.isfinite(started) and previous and previous != "complete":
            timings[previous] = round(timings.get(previous, 0) + max(0, now - started), 3)
        receipt["stage_timings"] = timings
        receipt["stage_started_at"] = now
        transitions = receipt.get("stage_transitions", [])
        receipt["stage_transitions"] = (transitions[-63:] if isinstance(transitions, list) else []) + [
            {"stage": stage, "at": now}]
    receipt["stage"] = stage
    _persist(path, receipt)
    _notify_progress(receipt, on_progress)


def _expanded_prompt(value, key, video_url):
    """Keep useful provider wording in the private receipt without credentials."""
    if not isinstance(value, str):
        return None
    secrets = {key}
    if isinstance(video_url, str):
        for values in parse_qs(urlparse(video_url).query).values():
            secrets.update(v for v in values if len(v) >= 4)
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[redacted]")
    value = re.sub(r"https?://\S+|data:[^\s]+", "[remote data]", value)
    value = re.sub(r"rk_[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_.-]+", "[credential]", value)
    return value[:50000]


def _finish_local(target, receipt, seconds, deadline, on_progress=None):
    """Keep the provider bytes and fit their complete timeline into the delivery cut."""
    output = target / "fal-camera.mp4"
    native = target / "fal-camera-native.mp4"
    receipt_path = target / "fal-request.json"
    if not native.is_file():
        if receipt.get("stage") not in {"completed", "complete", "normalizing"} or not output.is_file():
            raise FalError("This Fal request has no downloaded local video to recover.")
        # Older versions downloaded the provider file under the delivery filename.
        # Decode before renaming; this preserves the paid result without any HTTP.
        _verify_video(output, deadline, native=True)
        output.replace(native)
    _stage(receipt_path, receipt, "normalizing", on_progress)
    native_media = _verify_video(native, deadline, native=True)
    requested_resolution = receipt.get("parameters", {}).get("resolution")
    if requested_resolution == "1080P":
        resolution_check = {"requested": requested_resolution, "minimum_short_side": 1080,
                            "returned_width": native_media["width"], "returned_height": native_media["height"],
                            "verified": min(native_media["width"], native_media["height"]) >= 1080}
        receipt.update(native_file_name=native.name, native_media=native_media,
                       resolution_check=resolution_check)
        _persist(receipt_path, receipt)
        if not resolution_check["verified"]:
            raise FalResolutionError(
                f"Fal returned {native_media['width']}x{native_media['height']} for the requested 1080P output. "
                "Its native video and request ID are preserved. No local upscale or new request was made.")
    # Follow this run's saved request, including legacy requests after a preset change.
    # All deliveries before 1080P support used the 1280 by 720 canvas.
    width, height = (1920, 1080) if requested_resolution == "1080P" else (1280, 720)
    normalization = {"fit": "letterbox", "width": width, "height": height, "fps": 30,
                     "seconds": seconds, "native_video_seconds": native_media["video_duration"],
                     "retime_scale": seconds / native_media["video_duration"],
                     "retime_method": "uniform_full_video", "audio": "removed"}
    cached = receipt.get("normalization") == normalization and output.is_file()
    if not cached:
        receipt.update(native_file_name=native.name, native_media=native_media)
        _persist(receipt_path, receipt)
        temporary = target / "fal-camera-normalizing.mp4"
        filters = (f"setpts=(PTS-STARTPTS)*{normalization['retime_scale']:.12f},fps=30,"
                   f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                   f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                   f"tpad=stop_mode=clone:stop_duration=1,trim=duration={seconds}")
        try:
            media.run(["ffmpeg", "-y", "-v", "error", "-xerror", "-i", native,
                       "-map", "0:v:0", "-an", "-vf", filters, "-frames:v", str(seconds * 30),
                       "-c:v", "libx264", "-preset", media.LOCAL_ENCODE_PRESET,
                       "-threads", str(media.LOCAL_ENCODE_THREADS), "-crf", "18", "-pix_fmt", "yuv420p",
                       "-movflags", "+faststart", temporary], timeout=min(120, _remaining(deadline)))
            details = _verify_video(temporary, deadline)
            if (details["width"] != width or details["height"] != height or details["fps"] != 30
                    or abs(details["duration"] - seconds) > .001 or details["audio_codec"] is not None):
                raise FalError("The Fal delivery video did not match its requested timing and format.")
            temporary.replace(output)
        except FalError:
            raise
        except Exception:
            raise FalError("Could not normalize the downloaded Fal video. Its native file is preserved.") from None
        finally:
            temporary.unlink(missing_ok=True)
    else:
        details = _verify_video(output, deadline)
        if (details["width"] != width or details["height"] != height or details["fps"] != 30
                or abs(details["duration"] - seconds) > .001 or details["audio_codec"] is not None):
            raise FalError("The saved Fal delivery no longer matches its timing and format.")
    receipt.update(media=details, file_name=output.name,
                   native_file_name=native.name, native_media=native_media,
                   normalization=normalization, capture_method="provider_video_normalized", audio="none")
    _stage(receipt_path, receipt, "complete", on_progress)
    return {"path": str(output), "native_path": str(native), "request_id": receipt["request_id"],
            "media": details, "native_media": native_media, "normalization": normalization,
            "model": MODEL, "exact_camera_controls": True,
            "resolution_check": receipt.get("resolution_check"),
            "capture_method": "provider_video_normalized", "audio": "none",
            **_timing_details(receipt), "stage_transitions": receipt.get("stage_transitions", [])}


def recover_local(output_dir, *, on_progress=None):
    """Repair a downloaded acknowledged run locally, without keys or network access."""
    target = Path(output_dir).resolve()
    try:
        receipt = media.read_json(target / "fal-request.json", {})
        seconds = receipt["seconds"]
        request_id = receipt["request_id"]
        if (receipt.get("model") != MODEL or isinstance(seconds, bool) or not isinstance(seconds, int)
                or not 5 <= seconds <= 15 or not isinstance(request_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", request_id)
                or receipt.get("stage") not in {"completed", "complete", "downloading", "normalizing"}):
            raise ValueError("Invalid saved request")
    except (ValueError, KeyError, TypeError):
        raise FalError("This directory has no acknowledged completed Fal request to recover.") from None
    with LOCK:
        return _finish_local(target, receipt, seconds, time.monotonic() + 300, on_progress)


def generate(frame_path, output_dir, prompt, seconds=6, *, resume_only=False, on_progress=None,
             credential_key=ENV_CREDENTIAL, request_parameters=None, orbit_path=None):
    """Submit once per output directory; later calls resume the recorded request.

    A timeout never cancels or resubmits a paid request. A submit without an
    acknowledged ID stays ambiguous and requires manual review in Fal's dashboard.
    Stage timings measure observed elapsed time, including time disconnected on
    resume. Only provider_timings reports Fal's own inference measurements.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 5 <= seconds <= 15:
        raise FalError("Choose an integer Fal duration between 5 and 15 seconds.")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 50000:
        raise FalError("Supply a frozen-scene camera prompt of up to 50,000 characters.")
    try:
        image_bytes = Path(frame_path).read_bytes()
        if len(image_bytes) > 20 * 1024 * 1024:
            raise ValueError("Image too large")
        with Image.open(io.BytesIO(image_bytes)) as image:
            if image.format != "PNG" or image.width * 9 != image.height * 16:
                raise ValueError("Expected 16:9 PNG")
            image.verify()
    except Exception:
        raise FalError("Save a valid 16:9 PNG freeze frame before using Fal camera controls.") from None
    key = api_key(credential_key)
    credential_owner = hashlib.sha256(("fal-browser-v1\0" + key).encode()).hexdigest()
    image_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    if request_parameters is None:
        request = media.orbit_request(image_url, orbit_path=orbit_path or orbit_paths.DEFAULT)
    else:
        request = {**orbit_paths.parameters(request_parameters), "image_url": image_url}
        if request["duration"] != seconds:
            raise FalError("The saved motion request duration does not match this run.")
    request.update(prompt=prompt, duration=seconds)
    fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    target = Path(output_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    receipt_path = target / "fal-request.json"
    output = target / "fal-camera.mp4"
    native = target / "fal-camera-native.mp4"
    deadline = time.monotonic() + TIMEOUT_SECONDS
    with LOCK:
        if receipt_path.exists():
            try:
                receipt = media.read_json(receipt_path)
                if receipt.get("credential_source") == "browser" and (
                        credential_key is ENV_CREDENTIAL or receipt.get("credential_owner") != credential_owner):
                    raise FalError("Reconnect the original Fal key to resume this saved request.")
                # Acknowledged runs retain their own camera settings and resolution.
                # Reconstruct the hash with the supplied frame, prompt and duration
                # so a preset upgrade cannot block read-only historical recovery.
                if request_parameters is None and orbit_path is None and isinstance(receipt.get("parameters"), dict):
                    saved_request = dict(receipt["parameters"])
                    saved_request.update(image_url=request["image_url"], prompt=prompt, duration=seconds)
                    fingerprint = hashlib.sha256(json.dumps(saved_request, sort_keys=True).encode()).hexdigest()
                if receipt["input_sha256"] != fingerprint:
                    raise FalError("This Fal run belongs to a different saved frame or prompt.")
                if not receipt.get("request_id"):
                    raise FalError("This Fal submission has no acknowledged request ID. Check Fal's dashboard before a new run.")
            except (ValueError, KeyError, TypeError):
                raise FalError("The saved Fal request could not be read. No replacement request was submitted.") from None
        else:
            if resume_only:
                raise FalError("No acknowledged Fal receipt exists. Resume cannot submit a new generation.")
            receipt = {"provider": "Fal", "model": MODEL, "stage": "submitting",
                       "stage_started_at": time.time(), "stage_timings": {},
                       "preset_id": media.ORBIT_PRESET["id"], "prompt": prompt,
                       "input_sha256": fingerprint,
                       "orbit_path": orbit_path or orbit_paths.DEFAULT, "orbit_path_version": orbit_paths.VERSION,
                       "frame_sha256": hashlib.sha256(image_bytes).hexdigest(),
                       "seconds": seconds, "exact_camera_controls": True,
                       "parameters": {k: v for k, v in request.items() if k not in {"image_url", "prompt"}}}
            if credential_key is not ENV_CREDENTIAL:
                receipt.update(credential_source="browser", credential_owner=credential_owner)
            receipt["stage_transitions"] = [{"stage": "submitting", "at": receipt["stage_started_at"]}]
            try:
                with receipt_path.open("x") as stream:
                    json.dump(receipt, stream, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                raise FalError("This Fal run was already claimed. No duplicate was submitted.") from None
            _notify_progress(receipt, on_progress)
            submitted = _json_request(f"{QUEUE}/{MODEL}", key, deadline, request)
            request_id = submitted.get("request_id")
            if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", request_id):
                raise FalError("Fal did not acknowledge a request ID. No retry was submitted.")
            receipt.update(request_id=request_id)
            _stage(receipt_path, receipt, "queued", on_progress)
            receipt.update(status_url=_queue_url(submitted.get("status_url"), request_id, "status"),
                           response_url=_queue_url(submitted.get("response_url"), request_id, "result"))
            _persist(receipt_path, receipt)

    request_id = receipt["request_id"]
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", request_id):
        raise FalError("The saved Fal request ID is invalid. No replacement was submitted.")
    status_url = _queue_url(receipt.get("status_url"), request_id, "status")
    response_url = _queue_url(receipt.get("response_url"), request_id, "result")
    available_locally = (receipt.get("stage") in {"completed", "complete", "downloading", "normalizing"}
                         and (native.is_file() or output.is_file()))
    if not available_locally:
        while True:
            progress = _json_request(status_url, key, deadline)
            state = progress.get("status")
            if state not in {"IN_QUEUE", "IN_PROGRESS", "COMPLETED"}:
                raise FalError("Fal returned an unknown queue state. No retry was submitted.")
            receipt["provider_timings"] = {**_numeric_timings(receipt.get("provider_timings")),
                                           **_numeric_timings(progress.get("timings"))}
            _stage(receipt_path, receipt, "queued" if state == "IN_QUEUE" else state.lower(), on_progress)
            if state == "COMPLETED":
                if progress.get("error") or progress.get("error_type"):
                    raise FalError("Fal reported that this camera request failed. No retry was submitted.")
                break
            interval = ACTIVE_POLL_SECONDS if state == "IN_PROGRESS" else POLL_SECONDS
            time.sleep(min(interval, _remaining(deadline)))
        result = _json_request(response_url, key, deadline)
        video = result.get("video")
        if not isinstance(video, dict):
            raise FalError("Fal completed without returning a video. No retry was submitted.")
        receipt["expanded_prompt"] = _expanded_prompt(result.get("expanded_prompt"), key, video.get("url"))
        receipt["provider_timings"] = {**_numeric_timings(receipt.get("provider_timings")),
                                       **_numeric_timings(result.get("timings"))}
        _stage(receipt_path, receipt, "downloading", on_progress)

        def downloaded(progress):
            receipt["download_progress"] = progress
            _persist(receipt_path, receipt)
            _notify_progress(receipt, on_progress)

        _download(video.get("url"), native, deadline, on_progress=downloaded)
    with LOCK:
        finished = _finish_local(target, receipt, seconds, deadline, on_progress)
    # The server's public run projection deliberately does not copy this field.
    finished["expanded_prompt"] = receipt.get("expanded_prompt")
    if credential_key is ENV_CREDENTIAL:
        _persist(data_dir(ROOT) / "fal-status.json", {
            "key_fingerprint": hashlib.sha256(key.encode()).hexdigest(),
            "authenticated": True, "checked_at": int(time.time()), "model": MODEL})
    return finished
