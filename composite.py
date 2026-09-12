"""Assemble local source context around an already generated camera orbit."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from fractions import Fraction
from pathlib import Path

import media
from crowd_audio import AUDIO_VERSION

LEAD_SECONDS = media.DEFAULTS["lead_seconds"]
TAIL_OPTIONS = (media.DEFAULTS["tail_seconds"],)
OUTPUT_SIZE = (1920, 1080)
OUTPUT_FPS = 30
OUTPUT_SAMPLE_RATE = 48000
JOIN_FADE_SECONDS = .25


def _probe(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError("A required local media file is missing.")
    raw = json.loads(media.run(["ffprobe", "-v", "error", "-show_streams",
                                "-show_format", "-of", "json", path]))
    streams = raw.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return raw, video, audio


def _duration(stream, raw):
    try:
        value = float(stream.get("duration", raw["format"]["duration"]))
    except (KeyError, TypeError, ValueError):
        raise ValueError("Media duration could not be established.") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Media duration must be finite and positive.")
    return value


def _source_window(source, freeze_time, tail_seconds, resume_next_frame=False):
    if isinstance(tail_seconds, bool) or tail_seconds not in TAIL_OPTIONS:
        raise ValueError(f"Choose {media.DEFAULTS['tail_seconds']} seconds of resumed source action.")
    if isinstance(freeze_time, bool) or not math.isfinite(freeze_time):
        raise ValueError("Choose a finite source frame time.")
    raw, video, audio = _probe(source)
    if not video or not audio:
        raise ValueError("The source needs both video and original audio.")
    try:
        fps = Fraction(video["avg_frame_rate"])
        sample_rate = int(audio["sample_rate"])
    except (KeyError, ValueError, ZeroDivisionError):
        raise ValueError("The source frame and audio clocks could not be established.") from None
    if not 1 <= fps <= 240 or sample_rate <= 0:
        raise ValueError("Unsupported source frame or sample rate.")
    frame_index = round(Fraction(str(freeze_time)) * fps)
    freeze = float(frame_index / fps)
    resume_frame = frame_index + int(resume_next_frame)
    resume = float(resume_frame / fps)
    start, end = freeze - LEAD_SECONDS, resume + tail_seconds
    if start < 0 or end > min(_duration(video, raw), _duration(audio, raw)) + 0.000001:
        raise ValueError(f"This frame needs the full {LEAD_SECONDS} seconds before it and {tail_seconds} seconds after it.")
    return {"start": start, "freeze_time": freeze, "end": end,
            "freeze_frame": frame_index, "source_fps": str(fps),
            "source_size": [video["width"], video["height"]],
            "resume_time": resume, "resume_frame": resume_frame,
            "resume_next_frame": resume_next_frame,
            "source_sample_rate": sample_rate,
            "freeze_audio_sample": round(freeze * sample_rate),
            "resume_audio_sample": round(resume * sample_rate),
            "tail_seconds": tail_seconds}


def _output_size(value=None):
    if value is None:
        return OUTPUT_SIZE
    if not isinstance(value, (tuple, list)) or tuple(value) not in {OUTPUT_SIZE, (3840, 2160)}:
        raise ValueError("Choose a 1920 by 1080 or 3840 by 2160 delivery.")
    return tuple(value)


def _video_filter(duration, output_size=None):
    width, height = _output_size(output_size)
    return (f"trim=duration={duration:.9f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={OUTPUT_FPS},"
            f"tpad=stop_mode=clone:stop_duration={1 / OUTPUT_FPS:.9f},"
            f"trim=duration={duration:.9f},format=yuv420p")


def _audio_filter(duration):
    return (f"atrim=duration={duration:.9f},asetpts=PTS-STARTPTS,"
            f"aresample={OUTPUT_SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"apad,atrim=duration={duration:.9f}")


def _verify(path, expected_duration, endpoint_frames=None, output_size=None):
    raw, video, audio = _probe(path)
    if not video or not audio:
        raise RuntimeError("The finished highlight is missing video or audio.")
    duration = _duration(video, raw)
    if abs(duration - expected_duration) > 1 / OUTPUT_FPS + .005:
        raise RuntimeError("The finished highlight does not have its complete source context.")
    if (video["width"], video["height"]) != (output_size or OUTPUT_SIZE) or Fraction(video["avg_frame_rate"]) != OUTPUT_FPS:
        raise RuntimeError("The finished highlight has an unexpected video format.")
    if video["codec_name"] != "h264" or audio["codec_name"] != "aac" or int(audio["sample_rate"]) != OUTPUT_SAMPLE_RATE:
        raise RuntimeError("The finished highlight has an unexpected audio or video codec.")
    args = ["ffmpeg", "-v", "error", "-xerror", "-i", path]
    if endpoint_frames is None:
        args += ["-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"]
    else:
        first, last = endpoint_frames
        # The unfiltered branch and original audio continue through EOF. Collecting
        # the endpoint pixels here avoids decoding the first 14 seconds twice.
        args += ["-filter_complex", "[0:v:0]split=2[verify][samples];"
                 f"[samples]select='eq(n,{first})+eq(n,{last})'[selected]",
                 "-map", "[verify]", "-map", "0:a:0", "-f", "null", "-",
                 "-map", "[selected]", "-an", "-vsync", "0", "-pix_fmt", "rgb24",
                 "-f", "rawvideo", "pipe:1"]
    samples = media.run(args, timeout=600, strict_errors=True)
    details = {"duration": duration, "width": video["width"], "height": video["height"],
               "fps": OUTPUT_FPS, "video_codec": "h264", "audio_codec": "aac",
               "sample_rate": OUTPUT_SAMPLE_RATE, "bytes": Path(path).stat().st_size,
               "decoded_video": True, "decoded_audio": True}
    if endpoint_frames is not None:
        size = video["width"] * video["height"] * 3
        if len(samples) != size * 2:
            raise RuntimeError("Both orbit endpoint frames must decode completely.")
        details["endpoint_rgb_hashes"] = (hashlib.sha256(samples[:size]).hexdigest(),
                                          hashlib.sha256(samples[size:]).hexdigest())
    return details


def _temporary_output(output_path):
    target = Path(output_path).resolve()
    if target.suffix.lower() != ".mp4":
        raise ValueError("Finished highlights must use an MP4 output path.")
    if target.exists():
        raise FileExistsError("This finished output already exists; choose a new output path.")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".assembling-", suffix=".mp4", dir=target.parent)
    os.close(descriptor)
    return target, Path(name)


def _publish(temp, target):
    # Linking fails atomically if another worker has already published this path.
    os.link(temp, target)
    temp.unlink()


def _endpoint_hashes(path, first_frame, last_frame, width, height):
    pixels = media.run(["ffmpeg", "-v", "error", "-xerror", "-i", path, "-an", "-vf",
                        f"select=eq(n\\,{first_frame})+eq(n\\,{last_frame})",
                        "-vsync", "0", "-frames:v", "2", "-pix_fmt", "rgb24",
                        "-f", "rawvideo", "-"], timeout=180)
    frame_bytes = width * height * 3
    if len(pixels) != frame_bytes * 2:
        raise RuntimeError("Both orbit endpoint frames must decode completely.")
    return hashlib.sha256(pixels[:frame_bytes]).hexdigest(), hashlib.sha256(pixels[frame_bytes:]).hexdigest()


def assemble(source_path, orbit_path, freeze_time, output_path, tail_seconds=None, crowd_path=None,
             reference_anchored=False, auto_crowd=True, crowd_provenance=None, resume_next_frame=False,
             output_size=None):
    """Insert a decoded six-second orbit between the configured source segments.

    Provider audio is always discarded. An explicitly supplied crowd recording is
    looped beneath the orbit. Otherwise checked source atmosphere is extracted.
    Explicit auto_crowd=False retains the labelled silent diagnostic mode.
    Source action and original audio both resume from the saved source frame time.
    A completed output is immutable and cannot be overwritten by another assembly.
    Reference anchoring is verified on the input and after encoding; assembly never
    inserts or substitutes endpoint images to make an unanchored orbit pass.
    """
    started = time.perf_counter()
    output_size = _output_size(output_size)
    tail_seconds = media.DEFAULTS["tail_seconds"] if tail_seconds is None else tail_seconds
    if Path(output_path).exists():
        raise FileExistsError("This finished output already exists; choose a new output path.")
    source_path, orbit_path = Path(source_path).resolve(), Path(orbit_path).resolve()
    if not isinstance(resume_next_frame, bool):
        raise ValueError("Next-frame resumption must be explicitly true or false.")
    window = _source_window(source_path, freeze_time, tail_seconds, resume_next_frame)
    raw, video, _ = _probe(orbit_path)
    if not video:
        raise ValueError("The camera orbit needs a video track.")
    actual_orbit_duration = _duration(video, raw)
    if not 5.75 <= actual_orbit_duration <= 6.25:
        raise ValueError("The camera orbit must be approximately six seconds long.")
    if abs(video["width"] / video["height"] - 16 / 9) > .02:
        raise ValueError("The camera orbit must have a 16:9 frame.")
    orbit_seconds = round(actual_orbit_duration * OUTPUT_FPS) / OUTPUT_FPS
    first_orbit_frame = round(LEAD_SECONDS * OUTPUT_FPS)
    last_orbit_frame = first_orbit_frame + round(orbit_seconds * OUTPUT_FPS) - 1
    source_endpoint_hash = None
    if not isinstance(reference_anchored, bool):
        raise ValueError("Reference anchoring must be explicitly true or false.")
    if reference_anchored:
        if Fraction(video["avg_frame_rate"]) != OUTPUT_FPS or abs(actual_orbit_duration - orbit_seconds) > .001:
            raise ValueError("Normalize a reference-anchored orbit to 30 fps before assembling it.")
        first, last = _endpoint_hashes(orbit_path, 0, round(orbit_seconds * OUTPUT_FPS) - 1,
                                      video["width"], video["height"])
        if first != last:
            raise ValueError("The supplied orbit does not have matching decoded reference endpoints.")
        source_endpoint_hash = first
    generated_crowd = None
    if crowd_path is None and auto_crowd:
        import crowd_audio
        target_folder = Path(output_path).resolve().parent
        target_folder.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".crowd-bed-", suffix=".wav", dir=target_folder)
        os.close(descriptor)
        generated_crowd = Path(name)
        generated_crowd.unlink()
        try:
            prepared = crowd_audio.prepare_crowd_bed(source_path, window["freeze_time"], generated_crowd,
                                                     seconds=orbit_seconds + 2 * JOIN_FADE_SECONDS)
        except crowd_audio.CrowdUnavailable as error:
            generated_crowd.unlink(missing_ok=True)
            prepared = crowd_audio.fallback_bed()
            prepared["provenance"]["fallback_reason"] = str(error)
        crowd_path, crowd_provenance = Path(prepared["path"]), prepared["provenance"]
    if crowd_path is not None:
        crowd_path = Path(crowd_path).resolve()
        crowd_raw, _, crowd_audio = _probe(crowd_path)
        if not crowd_audio or _duration(crowd_audio, crowd_raw) < .1:
            raise ValueError("The crowd recording needs a nonempty audio track.")
    target, temp = _temporary_output(output_path)
    total = LEAD_SECONDS + orbit_seconds + tail_seconds
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-y",
            "-ss", f"{window['start']:.9f}", "-t", str(LEAD_SECONDS), "-i", source_path,
            "-i", orbit_path, "-ss", f"{window['resume_time']:.9f}",
            "-t", str(tail_seconds), "-i", source_path]
    if crowd_path is not None:
        args += ["-stream_loop", "-1", "-i", crowd_path]
    else:
        args += ["-f", "lavfi", "-i", f"anullsrc=r={OUTPUT_SAMPLE_RATE}:cl=stereo"]
    audio_lead = _audio_filter(LEAD_SECONDS)
    audio_tail = _audio_filter(tail_seconds)
    if crowd_path:
        audio_lead += f",afade=t=out:st={LEAD_SECONDS - JOIN_FADE_SECONDS}:d={JOIN_FADE_SECONDS}"
        audio_tail += f",afade=t=in:st=0:d={JOIN_FADE_SECONDS}"
    graph_parts = [
        f"[0:v:0]{_video_filter(LEAD_SECONDS, output_size)}[lead_v]",
        f"[0:a:0]{audio_lead}[lead_a]",
        f"[1:v:0]{_video_filter(orbit_seconds, output_size)}[orbit_v]",
        f"anullsrc=r={OUTPUT_SAMPLE_RATE}:cl=stereo,{_audio_filter(orbit_seconds)}[orbit_a]",
        f"[2:v:0]{_video_filter(tail_seconds, output_size)}[tail_v]",
        f"[2:a:0]{audio_tail}[tail_a]",
        "[lead_v][lead_a][orbit_v][orbit_a][tail_v][tail_a]concat=n=3:v=1:a=1[v][original_audio]",
    ]
    if crowd_path:
        bed_duration = orbit_seconds + 2 * JOIN_FADE_SECONDS
        delay = round((LEAD_SECONDS - JOIN_FADE_SECONDS) * 1000)
        graph_parts += [f"[3:a:0]{_audio_filter(bed_duration)},afade=t=in:st=0:d={JOIN_FADE_SECONDS},"
                        f"afade=t=out:st={bed_duration - JOIN_FADE_SECONDS}:d={JOIN_FADE_SECONDS},"
                        f"adelay={delay}|{delay},apad,atrim=duration={total}[bed_audio]",
                        "[original_audio][bed_audio]amix=inputs=2:duration=first:normalize=0[a]"]
    else:
        graph_parts.append("[original_audio]anull[a]")
    graph = ";".join(graph_parts)
    encoding = ["-crf", "19"]
    if reference_anchored:
        # Identical input pixels need identical intra-frame quantization. Contextual
        # CRF and adaptive quantization can otherwise change only one endpoint.
        encoding = ["-qp", "18", "-forced-idr", "1", "-force_key_frames",
                    f"expr:eq(n,{first_orbit_frame})+eq(n,{last_orbit_frame})",
                    "-x264-params", "aq-mode=0:psy=0:mbtree=0:ipratio=1"]
    args += ["-filter_complex_threads", "2", "-filter_complex", graph,
             "-map", "[v]", "-map", "[a]", "-t", f"{total:.9f}",
             "-c:v", "libx264", "-preset", media.LOCAL_ENCODE_PRESET, *encoding,
             "-threads", str(media.LOCAL_ENCODE_THREADS),
             "-c:a", "aac", "-b:a", "192k", "-ar", str(OUTPUT_SAMPLE_RATE),
             "-movflags", "+faststart", temp]
    try:
        encode_started = time.perf_counter()
        media.run(args, timeout=900)
        verify_started = time.perf_counter()
        details = _verify(temp, total, (first_orbit_frame, last_orbit_frame) if reference_anchored else None,
                          output_size=output_size)
        reference_closure = None
        if reference_anchored:
            first, last = details.pop("endpoint_rgb_hashes")
            if first != last:
                raise RuntimeError("The assembled orbit's decoded reference endpoints did not match.")
            reference_closure = {"reference_anchored": True, "first_last_pixel_hash_equal": True,
                                 "first_frame": first_orbit_frame, "last_frame": last_orbit_frame,
                                 "first_seconds": first_orbit_frame / OUTPUT_FPS,
                                 "last_seconds": last_orbit_frame / OUTPUT_FPS,
                                 "endpoint_rgb_sha256": first, "source_endpoint_rgb_sha256": source_endpoint_hash}
        verified_at = time.perf_counter()
        _publish(temp, target)
    finally:
        temp.unlink(missing_ok=True)
        if generated_crowd is not None:
            generated_crowd.unlink(missing_ok=True)
    orbit_end = LEAD_SECONDS + orbit_seconds
    return {"path": str(target), "media": details, "source_window": window,
            "timings": {"preparation": encode_started - started,
                        "encode": verify_started - encode_started,
                        "verification": verified_at - verify_started,
                        "total": time.perf_counter() - started},
            "encoding": {"preset": media.LOCAL_ENCODE_PRESET, "threads": media.LOCAL_ENCODE_THREADS,
                         "qp": 18 if reference_anchored else None, "crf": None if reference_anchored else 19},
            "provider_orbit_seconds": actual_orbit_duration,
            "resolution_provenance": {"output_size": list(output_size),
                                      "source_size": window["source_size"],
                                      "source_upscaled": any(a > b for a, b in zip(output_size, window["source_size"])),
                                      "orbit_input_size": [video["width"], video["height"]],
                                      "orbit_input_upscaled": output_size[0] > video["width"] or output_size[1] > video["height"]},
            "reference_closure": reference_closure,
            "audio_provenance": {"lead": "original_source", "tail": "original_source",
                                 "orbit": ("source_crowd_fallback" if crowd_provenance and crowd_provenance.get("reused_stadium_ambience") else
                                           "source_crowd_centre_suppressed" if crowd_provenance else
                                           "supplied_crowd_recording" if crowd_path else "silence_no_crowd_recording"),
                                 "crowd_bed": crowd_provenance,
                                 "audio_version": AUDIO_VERSION if crowd_path else "silent_diagnostic",
                                 "join_fade_seconds": JOIN_FADE_SECONDS if crowd_path else 0,
                                 "provider_audio_used": False},
            "segments": [
                {"kind": "source_lead", "output_start": 0, "output_end": LEAD_SECONDS,
                 "source_start": window["start"], "source_end": window["freeze_time"]},
                {"kind": "camera_orbit", "output_start": LEAD_SECONDS, "output_end": orbit_end},
                {"kind": "source_tail", "output_start": orbit_end, "output_end": total,
                 "source_start": window["resume_time"], "source_end": window["end"]},
            ]}


def combine(paths, output_path):
    """Combine highlights in order, copying picture and removing AAC padding at joins."""
    paths = [Path(path).resolve() for path in paths]
    if not paths:
        raise ValueError("Select at least one completed highlight for the combined reel.")
    durations = []
    output_size = None
    for path in paths:
        raw, video, audio = _probe(path)
        if not video or not audio:
            raise ValueError("Every combined highlight needs video and audio.")
        dimensions = (video["width"], video["height"])
        if output_size is not None and dimensions != output_size:
            raise ValueError("Combined highlights must have the same resolution. Select only 1080p clips or only 4K clips.")
        output_size = dimensions
        if output_size not in {OUTPUT_SIZE, (3840, 2160)}:
            raise ValueError("Combined highlights need a supported 1080p or 4K delivery resolution.")
        durations.append(_duration(video, raw))
        _verify(path, durations[-1], output_size=output_size)
    target, temp = _temporary_output(output_path)
    descriptor, filename = tempfile.mkstemp(prefix=".reel-", suffix=".txt", dir=target.parent)
    os.close(descriptor)
    listing = Path(filename)
    try:
        if any("\n" in str(path) or "\r" in str(path) for path in paths):
            raise ValueError("Media paths cannot contain line breaks.")
        listing.write_text("".join("file '" + str(path).replace("'", "'\\''") + "'\n" for path in paths))
        media.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-y",
                   "-f", "concat", "-safe", "0", "-i", listing, "-c:v", "copy",
                   "-af", "aresample=async=1:first_pts=0", "-c:a", "aac", "-b:a", "192k",
                   "-ar", str(OUTPUT_SAMPLE_RATE),
                   "-movflags", "+faststart", temp], timeout=900)
        details = _verify(temp, sum(durations), output_size=output_size)
        _publish(temp, target)
    finally:
        temp.unlink(missing_ok=True)
        listing.unlink(missing_ok=True)
    offset, segments = 0, []
    for path, duration in zip(paths, durations):
        segments.append({"path": str(path), "output_start": offset, "output_end": offset + duration})
        offset += duration
    return {"path": str(target), "media": details, "clips": segments}
