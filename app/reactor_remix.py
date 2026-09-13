"""One explicit X2 video remix, saved separately from its unmodified source.

Official contract: https://docs.reactor.inc/model-api-reference/x2/schema
X2 streams about 832p at 24 fps. Its API has no finite-file export or frame mapping;
matching the delivery duration does not establish exact source-frame alignment.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

from app import media
from app import reactor_api
from app.runtime_paths import PROJECT_ROOT
MODEL = "xmax/x2"
FPS = 24
TIMEOUT_SECONDS = 450
TAIL_CONTEXT_FRAMES = 96
STALL_SECONDS = 15
CAPACITY_MESSAGE = "Reactor is at capacity. Start again in a moment."


class RemixError(RuntimeError):
    pass


def _is_capacity_error(error):
    name = type(error).__name__.lower()
    return ("ratelimit" in name or getattr(error, "status", None) == 429
            or bool(re.search(r"\b429\b|no available capacity|at capacity", str(error), re.I)))


def _safe_error(error, credential_key=None):
    text = str(error)
    if isinstance(credential_key, str) and credential_key:
        text = text.replace(credential_key, "[private detail]")
    value = re.sub(r"https?://\S+|rk_[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_.-]+", "[private detail]", text)
    return value[:1200]


def _receipt_path(output):
    return output.with_suffix(".receipt.json")


def _request_path(output):
    return output.with_suffix(".request.json")


def _native_path(output):
    return output.with_name(output.stem + "-native.mp4")


def _transport_size(details):
    width, height = int(details["width"]), int(details["height"])
    scale = min(1, 1920 / width, 1080 / height)
    return max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)


def _video_info(path):
    raw = json.loads(media.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                "-show_streams", "-show_format", "-of", "json", path]))
    video = raw["streams"][0]
    numerator, denominator = map(int, video["avg_frame_rate"].split("/"))
    return {"width": video["width"], "height": video["height"],
            "fps": numerator / denominator, "frames": int(video.get("nb_frames", 0)),
            "duration": float(video.get("duration", raw["format"]["duration"])),
            "video_codec": video["codec_name"], "bytes": path.stat().st_size}


class Capture:
    """The SDK callback only queues decoded bytes; a bounded writer encodes them."""

    def __init__(self, path, expected_frames):
        self.path = path
        self.expected_frames = expected_frames
        self.pending = queue.Queue(maxsize=32)
        self.enabled = False
        self.dimensions = None
        self.frames = 0
        self.written = 0
        self.error = None
        self.first_timestamp_us = None
        self.last_timestamp_us = None
        self.first_frame_id = None
        self.last_frame_id = None
        self.process = None
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def on_frame(self, bgra, width, height, frame_id, timestamp_us, user_data):
        if not self.enabled or self.error or self.frames >= self.expected_frames:
            return
        if width < 2 or height < 2 or width > 4096 or height > 4096 or len(bgra) != width * height * 4:
            self.error = "The edited stream returned invalid pixels."
            return
        if self.dimensions is None:
            self.dimensions = (width, height)
        elif self.dimensions != (width, height):
            self.error = "The edited stream changed resolution during the remix."
            return
        try:
            self.pending.put_nowait(bgra)
        except queue.Full:
            self.error = "The local recorder could not keep every edited frame."
            return
        self.frames += 1
        if self.first_frame_id is None:
            self.first_frame_id = frame_id
            self.first_timestamp_us = timestamp_us
        self.last_frame_id = frame_id
        self.last_timestamp_us = timestamp_us

    def _write(self):
        try:
            while True:
                frame = self.pending.get()
                if frame is None:
                    break
                if self.process is None:
                    width, height = self.dimensions
                    self.process = subprocess.Popen(media._media_command([
                        "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "rawvideo",
                        "-pixel_format", "bgra", "-video_size", f"{width}x{height}",
                        "-framerate", str(FPS), "-i", "pipe:0", "-an", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", str(self.path)]), stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.process.stdin.write(frame)
                self.written += 1
            if self.process:
                self.process.stdin.close()
                if self.process.wait(timeout=30):
                    self.error = "The edited video could not be encoded."
        except Exception:
            self.error = "The local edited-video recorder stopped."
        finally:
            if self.process and self.process.poll() is None:
                self.process.kill()
                self.process.wait()

    def close(self, require_complete=True):
        self.enabled = False
        if self.thread.is_alive():
            try:
                self.pending.put(None, timeout=5)
            except queue.Full:
                self.error = "The recorder could not drain its frame queue."
            self.thread.join(timeout=35)
        if self.thread.is_alive():
            if self.process:
                self.process.kill()
            self.error = "The edited-video encoder exceeded its time limit."
        if require_complete and (self.error or self.written != self.expected_frames):
            raise RemixError(self.error or "The edited stream ended before the full clip was received.")
        return {"frames": self.written, "width": self.dimensions[0] if self.dimensions else None,
                "height": self.dimensions[1] if self.dimensions else None,
                "first_frame_id": self.first_frame_id, "last_frame_id": self.last_frame_id,
                "first_timestamp_us": self.first_timestamp_us,
                "last_timestamp_us": self.last_timestamp_us, "local_dropped_frames": 0 if not self.error else None}


async def _stream_source(source, track, details, count, save):
    width, height = _transport_size(details)
    decoder = await asyncio.create_subprocess_exec(*media._media_command([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-map", "0:v:0", "-an",
        "-vf", f"fps={FPS},scale={width}:{height}:flags=lanczos,setsar=1",
        "-frames:v", str(count), "-pix_fmt", "bgra", "-f", "rawvideo", "pipe:1"]),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        started = time.monotonic()
        for index in range(count):
            try:
                pixels = await decoder.stdout.readexactly(width * height * 4)
            except asyncio.IncompleteReadError:
                raise RemixError("The source did not decode to the complete expected timeline.") from None
            await asyncio.sleep(max(0, started + index / FPS - time.monotonic()))
            track.push_frame(pixels, width=width, height=height)
            if index % FPS == 0:
                save("remixing", source_frames_sent=index + 1)
        if await decoder.wait():
            raise RemixError("The source decoder could not finish.")
        save("remixing", source_frames_sent=count, source_stream_finished=True)
        return pixels, width, height
    finally:
        if decoder.returncode is None:
            decoder.kill()
            await decoder.wait()


async def _flush_source(track, last_frame, capture, save):
    """Continue the last image briefly so X2 can process its final source block.

    X2 documents continuous input, including a repeated still image, rather than
    an end-of-file command. Context frames are excluded by the capture frame cap.
    """
    pixels, width, height = last_frame
    sent = 0
    for index in range(TAIL_CONTEXT_FRAMES):
        if capture.frames >= capture.expected_frames or capture.error:
            break
        track.push_frame(pixels, width=width, height=height)
        sent += 1
        if index % FPS == 0:
            save("remixing", padding_frames_sent=index + 1)
        await asyncio.sleep(1 / FPS)
    save("remixing", padding_frames_sent=sent,
         source_padding_finished=True)


async def _session(request, receipt, save, *, credential_key=reactor_api.ENV_CREDENTIAL):
    from app.reactor_api import mint_token
    from reactor_sdk import Reactor

    client = None
    feeder = None
    padding = None
    capture = Capture(_native_path(Path(request["output"])), request["expected_frames"])
    failures = []

    def message(value):
        if not isinstance(value, dict):
            return
        data = value.get("data", {})
        if value.get("type") == "generation_started":
            capture.enabled = True
            save("remixing", generation_started=True,
                 declared_output_size=[data.get("width"), data.get("height")])
        elif value.get("type") == "command_error":
            failures.append("The video editor rejected its command. No retry was submitted.")

    try:
        async with asyncio.timeout(340):
            save("authenticating")
            token = await asyncio.to_thread(mint_token, model=MODEL, credential_key=credential_key)
            client = Reactor(model_name=MODEL, jwt=token["jwt"])
            client.on("message", message)
            client.on("error", lambda error: failures.append(
                CAPACITY_MESSAGE if _is_capacity_error(error) else "The video editing connection stopped."))
            client.track("main_video").on_raw_frame(capture.on_frame)
            save("connecting")
            await client.connect()
            save("preparing_stream", session_id=client.session_id)
            await client.send_command("set_keep_backlog", {"keep_backlog": True})
            track = await client.publish_track("source")
            # One prompt arms one run. This is never resent after an ambiguous failure.
            await client.send_command("set_prompt", {"prompt": request["prompt"]})
            feeder = asyncio.create_task(_stream_source(Path(request["source"]), track,
                                                        request["source_media"], request["expected_frames"], save))
            last_report = 0
            last_frames = 0
            last_frame_at = time.monotonic()
            while not feeder.done() or capture.frames < request["expected_frames"]:
                if failures:
                    raise RemixError(failures[0])
                if capture.error:
                    raise RemixError(capture.error)
                if feeder.done():
                    last_source_frame = feeder.result()
                    if padding is None:
                        last_frame_at = time.monotonic()
                        padding = asyncio.create_task(_flush_source(track, last_source_frame, capture, save))
                    elif padding.done():
                        padding.result()
                if capture.frames != last_frames:
                    last_frames, last_frame_at = capture.frames, time.monotonic()
                elif feeder.done() and time.monotonic() - last_frame_at > STALL_SECONDS:
                    raise RemixError("The edited stream stopped before its final frames arrived. The partial recording is saved; no retry was submitted.")
                if time.monotonic() - last_report >= .5:
                    save("remixing", frames_received=capture.frames,
                         percent=round(100 * capture.frames / request["expected_frames"], 1))
                    last_report = time.monotonic()
                await asyncio.sleep(.1)
            await feeder
            if padding:
                await padding
            capture.enabled = False
            save("saving_remix", frames_received=capture.frames, percent=100)
            await client.send_command("reset", {})
            receipt["capture"] = await asyncio.to_thread(capture.close)
    finally:
        if feeder and not feeder.done():
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
        if padding and not padding.done():
            padding.cancel()
            await asyncio.gather(padding, return_exceptions=True)
        receipt["capture"] = await asyncio.to_thread(capture.close, False)
        native = _native_path(Path(request["output"]))
        if native.exists():
            receipt["native_path"] = str(native)
        if client:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=15)
                receipt["disconnect_completed"] = True
            except Exception:
                receipt["disconnect_completed"] = False
        save(receipt.get("stage", "stopped"))


def _audio_hash(path):
    return media.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
                      "-c:a", "copy", "-f", "hash", "-hash", "sha256", "-"]).decode().strip()


def _finish(request, receipt, save):
    source, output = Path(request["source"]), Path(request["output"])
    native = _native_path(output)
    native_info = _video_info(native)
    if native_info["frames"] != request["expected_frames"]:
        raise RemixError("The recorded edit does not contain the complete expected frame count.")
    save("restoring_audio")
    # AAC is already present in finished app composites; copy all original packets.
    media.run(["ffmpeg", "-nostdin", "-v", "error", "-n", "-i", str(native), "-i", str(source),
               "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-map_metadata", "-1",
               "-movflags", "+faststart", str(output)], timeout=30)
    info = media.probe(output)
    source_audio = _audio_hash(source)
    if source_audio != _audio_hash(output):
        raise RemixError("The remix audio did not match the original audio packets.")
    if abs(info["duration"] - request["source_media"]["duration"]) > max(.1, 1 / FPS):
        raise RemixError("The remix duration differs from the original clip.")
    media.run(["ffmpeg", "-v", "error", "-xerror", "-err_detect", "explode", "-i", str(output),
               "-f", "null", "-"], timeout=40)
    receipt.update(path=str(output), native_path=str(native), native_media=native_info, media=info,
                   original_audio_preserved=True, audio_packet_sha256=source_audio,
                   audio="original_source_copy", decoded=True,
                   status="complete", completed_at=time.time())
    save("complete", percent=100)


def _worker(request_path, *, credential_key=reactor_api.ENV_CREDENTIAL):
    request = media.read_json(Path(request_path))
    output = Path(request["output"])
    receipt = {"status": "running", "model": MODEL, "provider": "Reactor",
               "source": request["source"], "source_media": request["source_media"],
               "source_sha256": request["source_sha256"], "prompt": request["prompt"],
               "credential_source": request.get("credential_source", "server"),
               "credential_owner": request.get("credential_owner"),
               "created_at": request["created_at"], "fps": FPS,
               "frames_expected": request["expected_frames"],
               "transport_size": list(_transport_size(request["source_media"])),
               "keep_backlog": True, "capture_method": "decoded_live_video",
               "timeline_preservation_verified": False, "frame_exact_capture": False,
               "quality_upscale": False, "original_audio_preserved": False}

    def save(stage, **fields):
        receipt.update(stage=stage, elapsed_seconds=round(time.time() - request["created_at"], 2), **fields)
        media.write_json(_receipt_path(output), receipt)

    try:
        if request.get("credential_source") == "browser" and credential_key is reactor_api.ENV_CREDENTIAL:
            raise RemixError("Reconnect the Reactor key that submitted this remix. No retry was submitted.")
        credential_key = reactor_api.resolve_key(credential_key)
        owner = request.get("credential_owner")
        if owner and owner != reactor_api.credential_fingerprint(credential_key):
            raise RemixError("This remix belongs to a different Reactor key. No retry was submitted.")
        asyncio.run(_session(request, receipt, save, credential_key=credential_key))
        _finish(request, receipt, save)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        public_error = (CAPACITY_MESSAGE if _is_capacity_error(error) else
                        _safe_error(error, credential_key) if isinstance(error, RemixError) else
                        "The remix stopped during " + receipt.get("stage", "setup") + ". No retry was submitted.")
        receipt.update(status="failed", error=public_error, diagnostic=_safe_error(error, credential_key),
                       error_type=type(error).__name__)
        save("failed")
        return 1


def generate(source: Path, output: Path, prompt: str, *, on_progress=None,
             credential_key=reactor_api.ENV_CREDENTIAL, credential_owner=None) -> dict:
    """Create one separate remix. Existing attempt files prohibit a paid retry."""
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_file() or source == output or output.suffix.lower() != ".mp4":
        raise RemixError("Choose an existing source and a separate MP4 output path.")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1000:
        raise RemixError("The remix prompt must contain 1 to 1000 characters.")
    if output.exists() or _native_path(output).exists() or _receipt_path(output).exists():
        raise RemixError("This remix attempt already exists. Its original result will not be replaced.")
    browser_owned = credential_key is not reactor_api.ENV_CREDENTIAL
    try:
        credential_key = reactor_api.resolve_key(credential_key)
    except reactor_api.ReactorError:
        raise RemixError("Connect your Reactor API key before remixing.") from None
    fingerprint = reactor_api.credential_fingerprint(credential_key)
    if credential_owner is not None and (not browser_owned or credential_owner != fingerprint):
        raise RemixError("This remix belongs to a different Reactor key. No retry was submitted.")
    details = media.probe(source)
    if not math.isfinite(details["duration"]) or not 5 <= details["duration"] <= 60:
        raise RemixError("Choose a finished clip between 5 and 60 seconds.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as stream:
        source_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    video_duration = _video_info(source)["duration"]
    request = {"source": str(source), "output": str(output), "prompt": prompt,
               "source_media": details, "source_sha256": source_hash,
               "expected_frames": round(video_duration * FPS), "created_at": time.time(),
               "credential_source": "browser" if browser_owned else "server",
               "credential_owner": fingerprint}
    try:
        with _request_path(output).open("x") as stream:
            json.dump(request, stream)
        _request_path(output).chmod(0o600)
    except FileExistsError:
        raise RemixError("This remix attempt was already submitted. It will not be resubmitted.") from None
    worker = None
    started = time.monotonic()
    last = None
    try:
        # The immutable key crosses the process boundary only in an anonymous pipe.
        # Never place it in arguments, inherited environment, or request files.
        worker = subprocess.Popen([sys.executable, "-m", "app.reactor_remix", "--worker",
                                   str(_request_path(output)), "--credential-stdin"],
                                  stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, start_new_session=True, cwd=PROJECT_ROOT)
        try:
            worker.stdin.write(credential_key.encode("ascii"))
        finally:
            worker.stdin.close()
        while worker.poll() is None:
            if time.monotonic() - started > TIMEOUT_SECONDS:
                raise RemixError("The remix exceeded its time limit. No retry was submitted.")
            current = media.read_json(_receipt_path(output), {})
            progress = {key: current[key] for key in ("stage", "percent", "frames_received", "frames_expected",
                                                     "elapsed_seconds", "session_id", "source_frames_sent") if key in current}
            if progress and progress != last and on_progress:
                on_progress(progress)
                last = progress
            time.sleep(.25)
    except BaseException as error:
        if worker is not None and worker.poll() is None:
            import signal
            os.killpg(worker.pid, signal.SIGTERM)
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(worker.pid, signal.SIGKILL)
                worker.wait()
        interrupted = media.read_json(_receipt_path(output), {})
        interrupted.update(status="failed", error=_safe_error(error, credential_key) or "The local remix wait was interrupted.",
                           disconnect_completed=False)
        media.write_json(_receipt_path(output), interrupted)
        raise
    receipt = media.read_json(_receipt_path(output), {})
    if worker.returncode or receipt.get("status") != "complete":
        raise RemixError(receipt.get("error", "The remix worker stopped. No retry was submitted."))
    if on_progress:
        on_progress({"stage": "complete", "percent": 100, "elapsed_seconds": receipt["elapsed_seconds"]})
    return receipt


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker" and sys.argv[3] == "--credential-stdin":
        # Missing, oversized or malformed pipe input fails closed in resolve_key.
        try:
            supplied_key = sys.stdin.buffer.read(513).decode("ascii")
        except (OSError, UnicodeError):
            supplied_key = None
        raise SystemExit(_worker(sys.argv[2], credential_key=supplied_key))
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        raise SystemExit(_worker(sys.argv[2]))
    raise SystemExit("This adapter is called by the local app.")
