"""Locally anchor an orbit's endpoints to its reference, without changing generation.

This edit does not verify a full camera turn or frozen subjects. It preserves the
generated interior and resolution, recording the chosen cut or legacy blend.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from fractions import Fraction
from pathlib import Path

from PIL import Image

from app import media
FPS = 30
START_BLEND_SECONDS = .15
END_BLEND_SECONDS = .20
END_HOLD_SECONDS = .15


def _probe(path):
    raw = json.loads(media.run(["ffprobe", "-v", "error", "-show_streams",
                                "-show_format", "-of", "json", path], timeout=60))
    video = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("The orbit needs a decodable video track.")
    duration = float(video.get("duration", raw["format"]["duration"]))
    fps = Fraction(video["avg_frame_rate"])
    if not math.isfinite(duration) or duration <= 0 or fps <= 0:
        raise ValueError("The orbit's video clock is invalid.")
    return raw, video, duration, fps


def _pixels(path, index):
    return media.run(["ffmpeg", "-v", "error", "-xerror", "-i", path, "-an", "-vf",
                      f"select=eq(n\\,{index})", "-frames:v", "1", "-pix_fmt", "rgb24",
                      "-f", "rawvideo", "-"], timeout=60)


def _verified_samples(path, frames, width=None, height=None):
    """Fully decode once while retaining only the three comparison frames."""
    if width is None or height is None:
        _, video, _, _ = _probe(path)
        width, height = video["width"], video["height"]
    samples = media.run([
        "ffmpeg", "-v", "error", "-xerror", "-i", path, "-filter_complex",
        "[0:v]split=2[verify][samples];"
        f"[samples]select='eq(n,0)+eq(n,{frames // 2})+eq(n,{frames - 1})'[selected]",
        "-map", "[verify]", "-an", "-f", "null", "-",
        "-map", "[selected]", "-an", "-vsync", "0", "-pix_fmt", "rgb24",
        "-f", "rawvideo", "pipe:1"], timeout=60)
    size = width * height * 3
    if len(samples) != size * 3:
        raise RuntimeError("The anchored output did not decode all required reference frames.")
    return samples[:size], samples[size:2 * size], samples[2 * size:]


def anchor_reference(orbit_path, frame_path, output_path, transition="blend", output_size=None):
    """Anchor the first and last frames, using an explicit cut or legacy blend.

    The first and final decoded frames must have identical RGB pixel hashes.
    Constant-QP intra coding makes both reference frames follow the same encoding
    path. An existing output or either input can never be overwritten.
    """
    started = time.perf_counter()
    orbit, reference, target = (Path(p).resolve() for p in (orbit_path, frame_path, output_path))
    if transition not in {"cut", "blend"}:
        raise ValueError("Choose a cut or blend reference transition.")
    if not orbit.is_file() or not reference.is_file():
        raise ValueError("The existing orbit and its selected reference PNG are required.")
    if target in {orbit, reference} or target.exists() or target.suffix.lower() != ".mp4":
        raise ValueError("Choose a new MP4 output path, separate from the reference and original orbit.")
    with Image.open(reference) as image:
        if image.format != "PNG" or image.width * 9 != image.height * 16:
            raise ValueError("The selected reference must be a 16:9 PNG.")
        reference_size = (image.width, image.height)
        image.verify()
    _, source_video, duration, source_fps = _probe(orbit)
    width, height = source_video["width"], source_video["height"]
    seconds = media.DEFAULTS["orbit_seconds"]
    frames = round(seconds * FPS)
    if (abs(duration - seconds) > .001 or source_fps != FPS
            or (width, height) not in {(1280, 720), (1920, 1080)}
            or int(source_video.get("nb_frames", 0)) != frames):
        raise ValueError("Normalize the orbit to six seconds, 16:9 at 720p or 1080p and 30 fps before anchoring it.")
    generated_size = (width, height)
    if output_size is not None:
        if not isinstance(output_size, (tuple, list)) or tuple(output_size) not in {(1920, 1080), (3840, 2160)}:
            raise ValueError("Choose a 1920 by 1080 or 3840 by 2160 reference delivery.")
        width, height = output_size
    orbit_hash = hashlib.sha256(orbit.read_bytes()).hexdigest()
    reference_hash = hashlib.sha256(reference.read_bytes()).hexdigest()
    fade_start = seconds - END_BLEND_SECONDS - END_HOLD_SECONDS
    hold_start = seconds - END_HOLD_SECONDS
    # A is the stationary reference, B is the generated video at its original time.
    # Both tracks are verified at 30fps. Frame-count timing also passed the interior
    # check where a timestamp-based expression held the looped reference throughout.
    clock = f"max(0,(N-1)/{FPS})"
    # Return the generated pixels directly throughout the interior. Computing the
    # same zero blend weight twice per pixel dominated the earlier expression.
    blend = (f"if(lt({clock},{START_BLEND_SECONDS}),A+(B-A)*({clock})/{START_BLEND_SECONDS},"
             f"if(lt({clock},{fade_start}),B,if(lt({clock},{hold_start}),"
             f"B+(A-B)*(({clock})-{fade_start})/{END_BLEND_SECONDS},A)))")
    filters = (f"[0:v]scale={width}:{height},setsar=1,format=yuv420p,"
               f"trim=duration={seconds},setpts=PTS-STARTPTS[reference];"
               f"[1:v]scale={width}:{height},setsar=1,format=yuv420p,setpts=PTS-STARTPTS[generated];"
               f"[reference][generated]blend=all_expr='{blend}':shortest=1[out]")
    if transition == "cut":
        filters = (f"[0:v]scale={width}:{height},setsar=1,format=yuv420p,"
                   "trim=end_frame=1,setpts=PTS-STARTPTS,split=2[first][last];"
                   f"[1:v]scale={width}:{height},setsar=1,format=yuv420p,trim=start_frame=1:end_frame={frames - 1},"
                   "setpts=PTS-STARTPTS[middle];"
                   f"[first][middle][last]concat=n=3:v=1:a=0,setpts=N/({FPS}*TB)[out]")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".reference-anchor-", suffix=".mp4", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        encode_started = time.perf_counter()
        media.run(["ffmpeg", "-y", "-v", "error", "-xerror", "-loop", "1", "-framerate", str(FPS),
                   "-i", reference, "-i", orbit, "-filter_complex_threads", "2",
                   "-filter_complex", filters, "-map", "[out]", "-an", "-frames:v", str(frames),
                   "-r", str(FPS), "-c:v", "libx264", "-preset", media.LOCAL_ENCODE_PRESET,
                   "-threads", str(media.LOCAL_ENCODE_THREADS), "-qp", "18",
                   "-g", "1", "-keyint_min", "1", "-sc_threshold", "0",
                   "-x264-params", "aq-mode=0:psy=0:mbtree=0:rc-lookahead=0:ipratio=1",
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart", temporary], timeout=180)
        verify_started = time.perf_counter()
        raw, video, output_duration, output_fps = _probe(temporary)
        if (abs(output_duration - seconds) > .001 or output_fps != FPS
                or int(video.get("nb_frames", 0)) != frames
                or (video["width"], video["height"]) != (width, height)
                or any(s.get("codec_type") == "audio" for s in raw["streams"])):
            raise RuntimeError("The reference-anchored delivery has an unexpected format.")
        first, middle, last = _verified_samples(temporary, frames, width, height)
        if len(first) != width * height * 3 or first != last:
            raise RuntimeError("The first and last decoded reference frames did not match.")
        if len(middle) != len(first) or middle == first:
            raise RuntimeError("The anchored output did not retain distinct generated interior content.")
        if (hashlib.sha256(orbit.read_bytes()).hexdigest() != orbit_hash
                or hashlib.sha256(reference.read_bytes()).hexdigest() != reference_hash):
            raise RuntimeError("An input changed during local reference anchoring.")
        # Publish without overwriting an output created by another local worker.
        os.link(temporary, target)
        return {"path": str(target), "reference_anchored": True,
                "timings": {"preparation": encode_started - started,
                            "encode": verify_started - encode_started,
                            "verification": time.perf_counter() - verify_started,
                            "total": time.perf_counter() - started},
                "encoding": {"preset": media.LOCAL_ENCODE_PRESET,
                             "threads": media.LOCAL_ENCODE_THREADS, "qp": 18},
                "transition": transition,
                "edit_version": "reference_cut_v1" if transition == "cut" else "reference_blend_v1",
                "reference_endpoint_frames": 1 if transition == "cut" else None,
                "added_hold_seconds": 0 if transition == "cut" else END_HOLD_SECONDS,
                "generated_motion_verified": False, "audio": "none",
                "source_orbit_sha256": orbit_hash, "reference_png_sha256": reference_hash,
                "first_last_pixel_hash_equal": True,
                "endpoint_rgb_sha256": hashlib.sha256(first).hexdigest(),
                "reference_render": {"width": width, "height": height, "pixel_format": "yuv420p"},
                "resolution_provenance": {"output_size": [width, height],
                                          "generated_size": list(generated_size),
                                          "reference_size": list(reference_size),
                                          "generated_upscaled": width > generated_size[0] or height > generated_size[1],
                                          "reference_upscaled": width > reference_size[0] or height > reference_size[1]},
                "blends": None if transition == "cut" else {"start": {"from": "reference", "to": "generated", "start": 0,
                                      "end": START_BLEND_SECONDS},
                           "end": {"from": "generated", "to": "reference", "start": fade_start,
                                    "end": hold_start},
                           "reference_hold": {"start": hold_start, "end": seconds}},
                "media": {"duration": output_duration, "width": width, "height": height,
                          "fps": FPS, "frames": frames, "video_codec": video["codec_name"],
                          "audio_codec": None, "bytes": target.stat().st_size, "decoded_video": True}}
    finally:
        temporary.unlink(missing_ok=True)
