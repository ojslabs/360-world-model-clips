"""Extract a checked stadium atmosphere bed by suppressing centred stereo sound."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, resample_poly, sosfiltfilt

import media
from runtime_paths import data_dir

SAMPLE_RATE = 48000
METHOD = "stereo_centre_suppression_v1"
AUDIO_VERSION = "source_crowd_v1"
MODEL_PATH = Path(os.environ.get("FOOTBALL_VAD_MODEL") or data_dir(Path(__file__).resolve().parent) / "audio-models/silero-vad-v6.1.onnx")
MODEL_URL = "https://raw.githubusercontent.com/snakers4/silero-vad/3f0c9ead5490e20f6a2ce7e9d16a53af9e0e5818/src/silero_vad/data/silero_vad.onnx"
MODEL_SHA256 = "597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004"
FALLBACK_PATH = data_dir(Path(__file__).resolve().parent) / "audio/fallback-crowd.wav"


class CrowdUnavailable(ValueError):
    """This valid source cannot provide an independently checked atmosphere bed."""


def isolate_stereo(samples):
    """Cancel common-channel material; never label a missing residual as atmosphere."""
    if samples.ndim != 2 or samples.shape[1] != 2 or len(samples) < SAMPLE_RATE:
        raise CrowdUnavailable("Automatic crowd atmosphere needs a stereo source. Supply a crowd recording for this clip.")
    if not np.isfinite(samples).all():
        raise ValueError("Source audio contains invalid samples.")
    centre = samples.mean(axis=1)
    side = (samples[:, 0] - samples[:, 1]) * .5
    source_rms = float(np.sqrt(np.mean(centre ** 2)))
    side = sosfiltfilt(butter(3, [100, 7500], btype="bandpass", fs=SAMPLE_RATE, output="sos"), side)
    residual_rms = float(np.sqrt(np.mean(side ** 2)))
    if residual_rms < .001 or residual_rms < source_rms * .025:
        raise CrowdUnavailable("This source has too little separate stereo atmosphere. Supply a crowd recording for this clip.")
    target_rms = min(.06, max(residual_rms, source_rms * .5))
    gain = min(2, target_rms / residual_rms, .90 / max(float(np.max(np.abs(side))), 1e-9))
    result = (side * gain).astype(np.float32)
    return result, {"method": METHOD, "source_centre_rms": source_rms,
                    "residual_rms_before_gain": residual_rms, "gain": gain,
                    "bed_rms": float(np.sqrt(np.mean(result ** 2))),
                    "bed_peak": float(np.max(np.abs(result))),
                    "bandpass_hz": [100, 7500], "perfect_speech_separation": False}


def _speech_seconds(paths):
    try:
        import onnxruntime
        if hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest() != MODEL_SHA256:
            raise ValueError("The speech model checksum does not match.")
        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = options.intra_op_num_threads = 1
        session = onnxruntime.InferenceSession(str(MODEL_PATH), sess_options=options, providers=["CPUExecutionProvider"])
        results = []
        for path in paths:
            rate, samples = wavfile.read(path)
            if rate != 16000 or samples.ndim != 1 or samples.dtype != np.int16:
                raise ValueError("Speech checks require mono 16kHz PCM.")
            samples = samples.astype(np.float32) / 32768
            state, context = np.zeros((2, 1, 128), np.float32), np.zeros((1, 64), np.float32)
            detected = 0
            for offset in range(0, len(samples), 512):
                chunk = samples[offset:offset + 512]
                joined = np.concatenate((context, np.pad(chunk, (0, 512 - len(chunk)))[None]), axis=1)
                probability, state = session.run(None, {"input": joined, "state": state, "sr": np.array(16000, np.int64)})
                context = joined[:, -64:]
                probability = float(probability.reshape(-1)[0])
                if not math.isfinite(probability):
                    raise ValueError("The speech model returned an invalid probability.")
                if probability >= .5:
                    detected += len(chunk)
            results.append(detected / 16000)
        return results
    except (ImportError, OSError, ValueError, RuntimeError):
        raise RuntimeError("The local speech check is unavailable. Run crowd_audio.py --setup-model or supply a reviewed crowd recording.") from None


def setup_model():
    """Explicit setup only; video assembly never downloads a dependency or model."""
    from urllib.request import urlopen
    body = urlopen(MODEL_URL, timeout=30).read(4 * 1024 * 1024)
    if hashlib.sha256(body).hexdigest() != MODEL_SHA256:
        raise RuntimeError("The downloaded speech model did not match the pinned checksum.")
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODEL_PATH.write_bytes(body)
    return {"path": str(MODEL_PATH), "sha256": MODEL_SHA256, "license": "MIT", "url": MODEL_URL}


def prepare_crowd_bed(source_path, freeze_time, output_path, seconds=6.5):
    """Write checked mono atmosphere without transmitting source audio anywhere."""
    source, target = Path(source_path).resolve(), Path(output_path).resolve()
    if target.exists() or target == source or target.suffix.lower() != ".wav":
        raise ValueError("Choose a new WAV path for the processed crowd atmosphere.")
    if not math.isfinite(freeze_time) or not math.isfinite(seconds) or not 1 <= seconds <= 20:
        raise ValueError("Choose a finite source time and a short crowd interval.")
    raw = json.loads(media.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", source]))
    track = next((s for s in raw.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not track or track.get("channels") != 2:
        raise CrowdUnavailable("Automatic crowd atmosphere needs a stereo source. Supply a crowd recording for this clip.")
    start = freeze_time - seconds / 2
    if start < 0 or start + seconds > float(raw["format"]["duration"]):
        raise ValueError("The source does not cover the required crowd interval.")
    pcm = media.run(["ffmpeg", "-v", "error", "-xerror", "-ss", f"{start:.9f}", "-i", source,
                     "-t", str(seconds), "-map", "0:a:0", "-vn", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"])
    samples = np.frombuffer(pcm, dtype="<f4").reshape(-1, 2)
    if abs(len(samples) - round(seconds * SAMPLE_RATE)) > 1:
        raise ValueError("The full crowd interval did not decode.")
    bed, provenance = isolate_stereo(samples)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".crowd-check-", dir=target.parent) as folder:
        scratch = Path(folder)
        for name, audio in (("source", samples.mean(axis=1)), ("bed", bed)):
            downsampled = np.clip(resample_poly(audio, 1, 3), -1, 1)
            wavfile.write(scratch / (name + ".wav"), 16000, np.round(downsampled * 32767).astype(np.int16))
        before, after = _speech_seconds([scratch / "source.wav", scratch / "bed.wav"])
        if after > .10:
            raise CrowdUnavailable("Speech remains in the processed atmosphere. Supply a reviewed crowd recording for this clip.")
        provenance.update(source_start=start, source_end=start + seconds, duration=seconds,
                          sample_rate=SAMPLE_RATE, source_speech_seconds=before, bed_speech_seconds=after,
                          speech_check="local_silero_onnx_v6.1", speech_check_passed=True,
                          speech_probability_threshold=.5, model_sha256=MODEL_SHA256, audio_version=AUDIO_VERSION,
                          crowd_identity="processed_source_atmosphere_not_classified")
        ready = scratch / "ready.wav"
        wavfile.write(ready, SAMPLE_RATE, np.round(np.clip(bed, -1, 1) * 32767).astype(np.int16))
        os.link(ready, target)
    return {"path": str(target), "provenance": provenance}


def configure_fallback(source_path, freeze_time, source_video_id):
    """Explicitly configure reusable checked atmosphere and record its actual source."""
    source_video_id = media.youtube_id(source_video_id)
    if FALLBACK_PATH.exists():
        return fallback_bed()
    result = prepare_crowd_bed(source_path, freeze_time, FALLBACK_PATH)
    result["provenance"].update(source_video_id=source_video_id, reused_stadium_ambience=True,
                                source_url=f"https://www.youtube.com/watch?v={source_video_id}",
                                file_sha256=hashlib.sha256(FALLBACK_PATH.read_bytes()).hexdigest())
    media.write_json(FALLBACK_PATH.with_suffix(".json"), result["provenance"])
    return result


def fallback_bed():
    """Read only a configured, checked recording; never improvise a silent fallback."""
    try:
        info = media.read_json(FALLBACK_PATH.with_suffix(".json"), {})
        if (not info.get("speech_check_passed") or info.get("bed_speech_seconds", 99) > .10
                or info.get("model_sha256") != MODEL_SHA256
                or not media.VIDEO_ID.fullmatch(info.get("source_video_id", ""))
                or info.get("file_sha256") != hashlib.sha256(FALLBACK_PATH.read_bytes()).hexdigest()):
            raise ValueError("The fallback crowd receipt is not valid.")
        rate, samples = wavfile.read(FALLBACK_PATH)
        if rate != SAMPLE_RATE or samples.dtype != np.int16 or len(samples) < SAMPLE_RATE:
            raise ValueError("The fallback crowd recording has an unexpected format.")
        if np.std(samples.astype(np.float32) / 32768) < .001:
            raise ValueError("The fallback crowd recording is empty.")
    except (OSError, ValueError, TypeError):
        raise CrowdUnavailable("This clip needs crowd atmosphere, and no verified fallback recording is configured.") from None
    return {"path": str(FALLBACK_PATH), "provenance": {**info, "reused_stadium_ambience": True}}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup-model", action="store_true", required=True)
    parser.parse_args()
    print(json.dumps(setup_model(), indent=2))
