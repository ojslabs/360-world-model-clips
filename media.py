"""Source-time media analysis and local, reviewable orbit request preparation."""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
from scipy.signal import find_peaks

ORBIT_PRESET = json.loads((Path(__file__).resolve().parent / "orbit_preset.json").read_text())
DEFAULTS = {"lead_seconds": 8, "orbit_seconds": ORBIT_PRESET["input"]["duration"], "tail_seconds": 4,
            "aspect_ratio": "16:9", "resolution": ORBIT_PRESET["input"]["resolution"]}
SOURCE_FORMAT = "bv*+ba/b"
SOURCE_SORT = "res,fps"
LOCAL_ENCODE_PRESET = "veryfast"
LOCAL_ENCODE_THREADS = min(8, os.cpu_count() or 1)
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
CUES = [(r"\btouchdown\b", "Touchdown", 1.0),
        (r"\bintercept(?:ed|ion)?\b", "Interception", 1.0),
        (r"\bfumble(?:d)?\b", "Fumble", .9),
        (r"\bwhat a (?:catch|play|tackle)\b", "Commentator reaction", .9),
        (r"\bsack(?:ed)?\b", "Sack", .7),
        (r"\b(?:caught|catch|tackle)\b", "Catch or tackle", .5),
        (r"\[(?:applause|cheering|crowd cheering)\]", "Captioned crowd reaction", .8)]


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temp.replace(path)


@lru_cache(maxsize=8)
def _ffmpeg_has_fps_mode(path, size, modified_ns, inode):
    """Cache capabilities against the selected executable's file identity."""
    result = subprocess.run([path, "-hide_banner", "-h", "full"], capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError("The configured FOOTBALL_FFMPEG executable could not report its options.")
    return bool(re.search(rb"(?m)^\s*-fps_mode(?:\s|\[)", result.stdout + result.stderr))


def _media_command(args):
    command = [str(value) for value in args]
    configured = os.environ.get("FOOTBALL_FFMPEG")
    if not command or command[0] != "ffmpeg" or not configured:
        return command
    binary = Path(configured)
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError("FOOTBALL_FFMPEG must name an executable file using an absolute path.")
    binary = binary.resolve()
    command[0] = str(binary)
    if "-vsync" in command:
        identity = binary.stat()
        if _ffmpeg_has_fps_mode(str(binary), identity.st_size, identity.st_mtime_ns, identity.st_ino):
            index = 1
            while index < len(command) - 1:
                if command[index:index + 2] == ["-vsync", "0"]:
                    command[index:index + 2] = ["-fps_mode", "passthrough"]
                index += 1
    return command


def run(args, timeout=300):
    result = subprocess.run(_media_command(args), capture_output=True, timeout=timeout)
    if result.returncode:
        detail = result.stderr.decode(errors="replace")[-1800:]
        detail = re.sub(r"https?://\S+", "[remote URL]", detail)
        raise RuntimeError(detail or f"{args[0]} exited {result.returncode}")
    return result.stdout


def youtube_id(value):
    value = value.strip()
    if VIDEO_ID.fullmatch(value):
        return value
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("Use a YouTube video link or video ID.")
    if parsed.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        parts = parsed.path.strip("/").split("/")
        result = (parse_qs(parsed.query).get("v", [""])[0] if parsed.path == "/watch"
                  else parts[1] if len(parts) == 2 and parts[0] in {"shorts", "live"} else "")
    elif parsed.hostname == "youtu.be":
        result = parsed.path.strip("/")
    else:
        result = ""
    if not VIDEO_ID.fullmatch(result):
        raise ValueError("Use a link to one YouTube video.")
    return result


def probe(path):
    raw = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                          "-of", "json", path]))
    video = next((s for s in raw["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in raw["streams"] if s["codec_type"] == "audio"), None)
    if not video or not audio:
        raise ValueError("The source needs both a video track and an audio track.")
    numerator, denominator = map(int, video["avg_frame_rate"].split("/"))
    fps = numerator / denominator if denominator else 0
    duration = float(raw["format"]["duration"])
    if not 1 <= fps <= 240 or not math.isfinite(duration) or duration < 5:
        raise ValueError("Unsupported source duration or frame rate.")
    return {"duration": duration, "width": video["width"], "height": video["height"],
            "fps": fps, "video_codec": video["codec_name"],
            "audio_codec": audio["codec_name"], "pixel_format": video.get("pix_fmt"),
            "color_transfer": video.get("color_transfer"),
            "bytes": path.stat().st_size}


def verify_source_decode(path, details):
    for time in (0, max(0, details["duration"] - 2)):
        run(["ffmpeg", "-v", "error", "-xerror", "-ss", str(time), "-i", path,
             "-t", "1", "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], timeout=120)


def validate_source_selection(info, details):
    """Compare selected quality with decoded media, including nominal NTSC labels."""
    selected = next((item for item in (info.get("requested_formats") or [])
                     if item.get("vcodec") != "none" and item.get("width") and item.get("height")), info)
    for dimension in ("width", "height"):
        expected = selected.get(dimension, info.get(dimension))
        if expected and expected != details[dimension]:
            raise RuntimeError("The downloaded source resolution does not match yt-dlp's selected quality.")
    expected = selected.get("fps")
    if expected is None:
        expected = info.get("fps")
    actual = details["fps"]
    match = "not_reported"
    if expected is not None:
        if (isinstance(expected, bool) or not isinstance(expected, (int, float))
                or not math.isfinite(expected) or expected <= 0):
            raise RuntimeError("yt-dlp's selected frame rate is invalid.")
        if abs(expected - actual) <= .01:
            match = "reported_precision"
        elif expected in {24, 30, 60, 120, 240} and math.isclose(
                actual, expected * 1000 / 1001, rel_tol=0, abs_tol=.000001):
            # YouTube reports 30 for a stream whose exact clock is 30000/1001.
            # Accept that named equivalence, not a broad percentage downgrade.
            match = "nominal_ntsc"
        else:
            raise RuntimeError("The downloaded source frame rate does not match yt-dlp's selected quality.")
    return {"selected_fps": expected, "measured_fps": actual, "comparison": match}


def browser_preview(source, directory, details, *, av1_mp4=False):
    """Keep full source dimensions and FPS, changing only incompatible encoding."""
    video_copy = details["video_codec"] == "h264" and details.get("pixel_format") in {None, "yuv420p"}
    video_copy |= av1_mp4 is True and details["video_codec"] == "av1"
    audio_copy = details["audio_codec"] in {"aac", "mp3"}
    output_video_codec = details["video_codec"] if video_copy else "h264"
    if source.suffix == ".mp4" and video_copy and audio_copy:
        return {"path": str(source), "kind": "original", "media": details,
                "resolution_preserved": True, "fps_preserved": True,
                "video_bitstream_preserved": True, "audio_transcoded": False}
    stem = "source-preview-av1" if output_video_codec == "av1" else "source-preview"
    target = directory / f"{stem}.mp4"
    fingerprint = {"source_file": source.name, "bytes": source.stat().st_size,
                   "mtime_ns": source.stat().st_mtime_ns, "version": 1,
                   "video_codec": output_video_codec}
    saved = read_json(directory / f"{stem}.json", {})
    cached_source = dict(saved.get("source", {}))
    if "video_codec" not in cached_source:
        cached_source["video_codec"] = saved.get("media", {}).get("video_codec")
    kind = "remux" if video_copy and audio_copy else "transcode"
    if target.is_file() and cached_source == fingerprint:
        preview = probe(target)
    else:
        temporary = directory / f"{stem}-pending.mp4"
        command = ["ffmpeg", "-y", "-v", "error", "-xerror", "-i", source,
                   "-map", "0:v:0", "-map", "0:a:0", "-sn", "-dn"]
        if video_copy:
            command += ["-c:v", "copy"]
        else:
            command += ["-c:v", "libx264", "-preset", "fast", "-crf", "17", "-pix_fmt", "yuv420p"]
            if details.get("color_transfer") in {"smpte2084", "arib-std-b67"}:
                if "zscale" not in run(["ffmpeg", "-hide_banner", "-filters"]).decode(errors="replace"):
                    raise RuntimeError("The highest-quality HDR original is preserved. Creating its browser preview requires FFmpeg with zscale support; no lower-quality source was downloaded.")
                command += ["-vf", "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                            "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p",
                            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
        command += ["-c:a", "copy"] if audio_copy else ["-c:a", "aac", "-b:a", "320k"]
        command += ["-movflags", "+faststart", temporary]
        try:
            run(command, timeout=max(300, min(14400, details["duration"] * 12)))
            preview = probe(temporary)
            verify_source_decode(temporary, preview)
            _verify_preview_match(details, preview)
            temporary.replace(target)
            write_json(directory / f"{stem}.json", {"source": fingerprint, "kind": kind, "media": preview})
        finally:
            temporary.unlink(missing_ok=True)
    _verify_preview_match(details, preview)
    if preview["video_codec"] != output_video_codec:
        raise RuntimeError("The saved browser preview uses a different video codec than requested.")
    return {"path": str(target), "kind": kind, "media": preview,
            "resolution_preserved": True, "fps_preserved": True,
            "video_bitstream_preserved": video_copy, "audio_transcoded": not audio_copy,
            "hdr_to_sdr": not video_copy and details.get("color_transfer") in {"smpte2084", "arib-std-b67"}}


def _verify_preview_match(source, preview):
    # Matroska's millisecond packet timestamps can slightly perturb MP4's measured
    # average rate during a lossless remux (for example, 60 becomes 60.0118).
    if ((preview["width"], preview["height"]) != (source["width"], source["height"])
            or abs(preview["fps"] - source["fps"]) > max(.001, source["fps"] * .0005)
            or abs(preview["duration"] - source["duration"]) > max(.15, 2 / source["fps"])):
        raise RuntimeError("The browser preview changed source resolution, frame rate or timing.")


def clock_seconds(value):
    h, m, s = value.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def captions(path):
    """Keep each newly spoken caption line once, retaining its source-video time."""
    if not path.exists():
        return []
    entries, recent = [], []
    for block in re.split(r"\n\s*\n", path.read_text().replace("\r", "")):
        lines = block.splitlines()
        timing = next((i for i, line in enumerate(lines) if " --> " in line), None)
        if timing is None:
            continue
        start, end = lines[timing].split(" --> ")
        start, end = clock_seconds(start), clock_seconds(end.split()[0])
        clean = [html.unescape(re.sub(r"<[^>]*>", "", line)).strip()
                 for line in lines[timing + 1:]]
        fresh = [line for line in clean if line and line not in recent]
        if fresh:
            entries.append({"start": start, "end": end, "text": " ".join(fresh)})
        recent = [line for line in clean if line][-3:]
    return entries


def commentary_events(entries):
    events = []
    for entry in entries:
        matches = [(label, weight) for pattern, label, weight in CUES
                   if re.search(pattern, entry["text"], re.I)]
        if matches:
            label, weight = max(matches, key=lambda x: x[1])
            events.append({"time": entry["start"], "label": label,
                           "cue": entry["text"], "score": weight, "kind": "commentary"})
    return events


def audio_activity(source, folder):
    pcm = folder / "analysis.f32"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", source,
         "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", pcm], timeout=600)
    samples = np.fromfile(pcm, dtype=np.float32)
    hop, sr = 4000, 16000
    count = len(samples) // hop
    if count < 4:
        raise ValueError("Audio is too short to analyze.")
    rms = np.sqrt(np.mean(samples[:count * hop].reshape(-1, hop) ** 2, axis=1))
    db = 20 * np.log10(np.maximum(rms, 1e-8))
    low, high = np.percentile(db, [10, 98])
    energy = np.clip((db - low) / max(float(high - low), 1), 0, 1)
    peaks, props = find_peaks(energy, height=.62, prominence=.10, distance=24)
    events = [{"time": round((float(index) + .5) * hop / sr, 3),
               "score": float(energy[index]) * .55, "label": "Audio peak",
               "cue": "Raised audio level; crowd identity needs review.", "kind": "audio"}
              for index in peaks]
    waveform = [{"time": round((i + .5) * hop / sr, 3), "energy": round(float(v), 4)}
                for i, v in enumerate(energy)]
    return events, waveform


def candidates(events, duration, limit=40):
    """Rank signals, merge nearby evidence, then retain chronological source time."""
    groups = []
    for event in sorted(events, key=lambda e: e["score"], reverse=True):
        if not 3 <= event["time"] <= duration - 3:
            continue
        match = next((g for g in groups if abs(g["time"] - event["time"]) < 5), None)
        if match is not None:
            match["signals"].append(event)
        else:
            groups.append({"time": event["time"], "signals": [event]})
    for group in groups:
        group["score"] = min(1.5, sum(s["score"] for s in group["signals"]))
    selected = sorted(groups, key=lambda g: g["score"], reverse=True)[:limit]
    result = []
    for group in sorted(selected, key=lambda g: g["time"]):
        speech = [s for s in group["signals"] if s["kind"] == "commentary"]
        best = max(speech or group["signals"], key=lambda s: s["score"])
        t = group["time"]
        result.append({"id": hashlib.sha256(f"{t:.3f}".encode()).hexdigest()[:12],
                       "time": t, "window_start": max(0, t - 6),
                       "window_end": min(duration, t + 7), "label": best["label"],
                       "cue": best["cue"], "signals": group["signals"], "selected": False})
    return result


def cut_window(time, duration, fps, tail=None):
    tail = DEFAULTS["tail_seconds"] if tail is None else tail
    if not math.isfinite(time) or not math.isfinite(tail) or tail != DEFAULTS["tail_seconds"]:
        raise ValueError(f"Choose a valid frame and {DEFAULTS['tail_seconds']} seconds of resumed action.")
    time = round(time * fps) / fps
    start, end = time - DEFAULTS["lead_seconds"], time + tail
    if start < 0 or end > duration:
        raise ValueError("This frame needs more source context for the lead-in and resumed action.")
    return {"freeze_time": time, "start": start, "end": end, "tail_seconds": tail,
            "source_seconds": end - start,
            "final_seconds": end - start + DEFAULTS["orbit_seconds"]}


def extract_frame(source, time, target):
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{time:.9f}",
         "-i", source, "-frames:v", "1", "-update", "1", target])


def orbit_request(image_url):
    request = json.loads(json.dumps(ORBIT_PRESET["input"]))
    request["image_url"] = image_url
    return request


def source_cut(source, window, target):
    details = probe(source)
    # The smallest exact 16:9 canvas that contains every original source pixel.
    unit = math.ceil(max(details["width"] / 32, details["height"] / 18))
    width, height = unit * 32, unit * 18
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(window["start"]),
         "-i", source, "-t", str(window["source_seconds"]),
         "-vf", f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1",
         "-c:v", "libx264", "-preset", "fast", "-crf", "17", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "320k",
         "-movflags", "+faststart", target])
