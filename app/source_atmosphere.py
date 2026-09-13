"""Make a short, slowed loop from the selected source's own pre-freeze audio."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from pathlib import Path
import tempfile
import time

import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly

from app import media
METHOD = "source_slow_motion_loop_v3"
SOURCE_SECONDS = 1.75
PLAYBACK_SPEED = .75
SPEED_RAMP_SECONDS = .75
LOOP_CROSSFADE_SECONDS = .15
SAMPLE_RATE = 48000


def _fingerprint(source):
    stat = source.stat()
    return {"path": str(source), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "inode": stat.st_ino}


def _loop(samples, frames, overlap):
    output = samples.copy()
    fade = np.linspace(0, 1, overlap, endpoint=False, dtype=np.float32)[:, None]
    joins = []
    while len(output) < frames:
        start = len(output) - overlap
        joined = output[-overlap:] * (1 - fade) + samples[:overlap] * fade
        output = np.concatenate((output[:-overlap], joined, samples[overlap:]))
        joins.append(start / SAMPLE_RATE)
    return output[:frames], joins


def _speed_positions(frames):
    clock = np.arange(frames, dtype=np.float64) / SAMPLE_RATE
    duration = frames / SAMPLE_RATE
    ramp = min(SPEED_RAMP_SECONDS, duration / 2)
    entrance = np.clip(clock / ramp, 0, 1)
    exit_progress = np.clip((clock - (duration - ramp)) / ramp, 0, 1)
    speed = (1 - (1 - PLAYBACK_SPEED) * .5 * (1 - np.cos(np.pi * entrance))
             + (1 - PLAYBACK_SPEED) * .5 * (1 - np.cos(np.pi * exit_progress)))
    # Trapezoidal integration makes pitch follow the continuous speed envelope.
    positions = np.zeros(frames, dtype=np.float64)
    positions[1:] = np.cumsum((speed[:-1] + speed[1:]) * .5)
    return positions, ramp


def _strict_media(args):
    """A clean exit and no error-level decoder messages are both required."""
    result = subprocess.run(media._media_command(args), capture_output=True, timeout=60)
    if result.returncode or result.stderr.strip():
        raise ValueError("Could not decode the original source audio. Its recording was preserved.")
    return result.stdout


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _stream_duration(stream, fallback=None):
    direct = _number(stream.get("duration"))
    if direct is not None:
        return direct
    tag = stream.get("tags", {}).get("DURATION")
    if isinstance(tag, str):
        try:
            hours, minutes, seconds = map(float, tag.split(":"))
            duration = hours * 3600 + minutes * 60 + seconds
            if math.isfinite(duration):
                return duration
        except ValueError:
            pass
    return _number(fallback)


def _source_window(source, freeze_time):
    """Read this window, distinguishing an absent track, clean EOF and failure."""
    try:
        details = json.loads(_strict_media(["ffprobe", "-v", "error", "-show_streams",
                                           "-show_format", "-of", "json", source]))
        streams = details["streams"]
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        primary = video if video is not None else audio
        if primary is None:
            raise ValueError("No playable media stream")
        duration = _stream_duration(primary, details.get("format", {}).get("duration"))
        if duration is None or duration <= 0 or freeze_time > duration + 1 / SAMPLE_RATE:
            raise ValueError("The freeze frame is outside the original source timeline.")
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ValueError("The original source streams could not be read.") from None
    expected = round(SOURCE_SECONDS * SAMPLE_RATE)
    if audio is None:
        return np.zeros((expected, 2), dtype=np.float32), b"", {
            "source_audio_present": False, "source_audio_channels": 0,
            "source_window_state": "no_audio_stream", "silence_reason": "no_audio_stream",
            "extracted_source_seconds": 0, "padded_source_seconds": SOURCE_SECONDS,
            "padding_reason": "no_audio_stream"}
    raw = _strict_media(["ffmpeg", "-v", "error", "-xerror", "-ss", f"{freeze_time - SOURCE_SECONDS:.9f}",
                         "-i", source, "-t", str(SOURCE_SECONDS), "-map", "0:a:0", "-vn",
                         "-af", f"aresample={SAMPLE_RATE}:async=1:first_pts=0", "-ac", "2",
                         "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"])
    if len(raw) % 8:
        raise ValueError("The decoded source audio has an incomplete sample frame.")
    samples = np.frombuffer(raw, dtype="<f4").reshape(-1, 2)
    if len(samples) > expected or not np.isfinite(samples).all():
        raise ValueError("The decoded source audio contains invalid samples or timing.")
    count = len(samples)
    origin = _number(details.get("format", {}).get("start_time")) or 0
    audio_start = (_number(audio.get("start_time")) or 0) - origin
    audio_duration = _number(audio.get("duration"))
    tagged_end = _stream_duration(audio)
    # Matroska DURATION tags encode the ending timestamp, unlike stream.duration.
    audio_end = (audio_start + audio_duration if audio_duration is not None else
                 tagged_end - origin if tagged_end is not None else None)
    if count < expected:
        # Known stream metadata must agree with a clean end of input. Do not
        # disguise missing decoded data in the middle of a recording as silence.
        if audio_end is not None and audio_end > freeze_time + .05:
            raise ValueError("The source audio ended unexpectedly before the selected window was decoded.")
        samples = np.pad(samples, ((0, expected - count), (0, 0)))
    is_silent = not np.any(samples)
    state = ("audio_ended" if count == 0 else "partial_audio_eof" if count < expected else
             "silent" if is_silent else "audible")
    return samples, raw, {"source_audio_present": True, "source_audio_channels": audio.get("channels"),
                         "source_window_state": state,
                         "silence_reason": "audio_ended" if count == 0 else "silent_source_window" if is_silent else None,
                         "extracted_source_seconds": count / SAMPLE_RATE,
                         "padded_source_seconds": (expected - count) / SAMPLE_RATE,
                         "padding_reason": "clean_audio_eof" if count < expected else None}


def prepare_slow_source_bed(source, freeze_time, output_dir, seconds=6.5):
    """Loop pre-freeze audio while easing from 1x to 0.75x and back to 1x.

    Uses the source as heard, including any speech. It does not classify or separate
    commentary, and never substitutes another recording or amplifies the samples.
    """
    started = time.perf_counter()
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError("The original source audio is required for this loop.")
    if (isinstance(freeze_time, bool) or not isinstance(freeze_time, (int, float))
            or not math.isfinite(freeze_time) or freeze_time < SOURCE_SECONDS):
        raise ValueError("Choose a frame with at least 1.75 seconds of preceding source audio.")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 1 <= seconds <= 30:
        raise ValueError("The source audio loop must be between 1 and 30 seconds.")
    identity = {"source": _fingerprint(source), "freeze_time": freeze_time,
                "seconds": seconds, "method": METHOD, "source_seconds": SOURCE_SECONDS,
                "minimum_playback_speed": PLAYBACK_SPEED, "speed_ramp_seconds": SPEED_RAMP_SECONDS,
                "speed_curve": "raised_cosine", "sample_rate": SAMPLE_RATE,
                "crossfade_seconds": LOOP_CROSSFADE_SECONDS}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"source-audio-{key}.wav"
    receipt_path = target.with_suffix(".json")
    receipt = media.read_json(receipt_path, {})
    if target.is_file() and receipt.get("identity") == identity:
        if hashlib.sha256(target.read_bytes()).hexdigest() == receipt.get("provenance", {}).get("bed_sha256"):
            return {"path": str(target), "provenance": receipt["provenance"]}
        raise ValueError("The saved source audio loop changed; preserve it and choose a new cache directory.")
    if target.exists():
        raise ValueError("An existing source audio loop has no matching receipt; choose a new cache directory.")
    source_start = freeze_time - SOURCE_SECONDS
    samples, raw, window_provenance = _source_window(source, freeze_time)
    source_rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    positions, ramp = _speed_positions(round(seconds * SAMPLE_RATE))
    repeated, source_joins = _loop(samples, math.ceil(positions[-1]) + 2,
                                   round(LOOP_CROSSFADE_SECONDS * SAMPLE_RATE))
    # Fourfold bandlimited oversampling keeps fractional-speed interpolation clean.
    oversample = 4
    oversampled = resample_poly(repeated, oversample, 1, axis=0).astype(np.float32)
    coordinates = np.arange(len(oversampled))
    bed = np.column_stack([np.interp(positions * oversample, coordinates, oversampled[:, channel])
                           for channel in range(2)]).astype(np.float32)
    joins = [float(np.searchsorted(positions, join * SAMPLE_RATE)) / SAMPLE_RATE for join in source_joins]
    peak = float(np.max(np.abs(bed)))
    gain = min(1, .9 / max(peak, 1e-9))
    bed *= gain
    if _fingerprint(source) != identity["source"]:
        raise RuntimeError("The source changed while its audio loop was being prepared.")
    descriptor, name = tempfile.mkstemp(prefix=".source-audio-", suffix=".wav", dir=directory)
    os.close(descriptor)
    temporary = Path(name)
    try:
        wavfile.write(temporary, SAMPLE_RATE, np.rint(bed * 32767).astype(np.int16))
        # Inspect the actual written samples rather than claiming duration from intent.
        rate, decoded = wavfile.read(temporary)
        if rate != SAMPLE_RATE or decoded.shape != (round(seconds * SAMPLE_RATE), 2):
            raise RuntimeError("The source audio loop has an unexpected audio format.")
        provenance = {"method": METHOD, "source_file": source.name,
                      "source_video_id": source.parent.name if media.VIDEO_ID.fullmatch(source.parent.name) else None,
                      "source_start": source_start, "source_end": freeze_time,
                      "source_seconds": SOURCE_SECONDS, **window_provenance,
                      "speed_envelope": {"curve": "raised_cosine", "start": 1,
                                         "minimum": PLAYBACK_SPEED, "end": 1, "ramp_seconds": ramp,
                                         "hold_start": ramp, "hold_end": seconds - ramp},
                      "pitch_follows_speed": True, "loop_crossfade_source_seconds": LOOP_CROSSFADE_SECONDS,
                      "loop_joins": joins, "seconds": len(decoded) / rate,
                      "sample_rate": rate, "channels": 2, "gain": gain,
                      "source_rms": source_rms,
                      "bed_rms": float(np.sqrt(np.mean((decoded.astype(np.float64) / 32768) ** 2))),
                      "bed_peak": float(np.max(np.abs(decoded.astype(np.float64)))) / 32768,
                      "bed_sha256": hashlib.sha256(temporary.read_bytes()).hexdigest(),
                      "source_pcm_sha256": hashlib.sha256(raw).hexdigest(),
                      "source_fingerprint": identity["source"], "reused_stadium_ambience": False,
                      "speech_separated": False, "classification": "whole_source_mix" if window_provenance["source_audio_present"] else "source_without_audio",
                      "preparation_seconds": time.perf_counter() - started}
        os.link(temporary, target)
        media.write_json(receipt_path, {"identity": identity, "provenance": provenance})
        return {"path": str(target), "provenance": provenance}
    finally:
        temporary.unlink(missing_ok=True)
