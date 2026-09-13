"""Source-camera-relative positions for the documented H3 camera keyframe schema.

Schema: https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=minimax/h3-max/camera-controls
Elevation is limited to [-90, 90]; there is no roll or up-vector input. These are
requested positions, not verification of the generated camera or subject motion.
"""
from __future__ import annotations

import copy
import math

DEFAULT = "around"
VERSION = "orbit-paths-v1"
_OPTIONS = (
    {"id": "around", "label": "Around", "description": "A full horizontal turn around the scene."},
    {"id": "over-under", "label": "Over & under",
     "description": "A vertical loop over and below the scene.",
     "experimental": True, "note": "The view may flip near the top or bottom."},
    {"id": "diagonal", "label": "Diagonal",
     "description": "A full turn tilted 45 degrees, moving above and below the starting view."},
)


def catalog():
    return {"default": DEFAULT, "version": VERSION, "options": copy.deepcopy(list(_OPTIONS)),
            "note": "Paths are relative to the starting view, not an automatically detected direction of travel."}


def selection(path_id):
    chosen = next((item for item in _OPTIONS if item["id"] == path_id), None)
    if chosen is None:
        raise ValueError("Choose Around, Over & under, or Diagonal.")
    return dict(chosen)


def validate(frames):
    if not isinstance(frames, list) or not 2 <= len(frames) <= 12:
        raise ValueError("A motion path must contain 2 to 12 keyframes.")
    previous_time = -1
    previous_azimuth = None
    travel = 0
    for frame in frames:
        if not isinstance(frame, dict) or set(frame) != {"time", "azimuth", "elevation", "distance"}:
            raise ValueError("A motion keyframe must specify time, azimuth, elevation and distance.")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in frame.values()):
            raise ValueError("Motion keyframes must contain finite numbers.")
        if not previous_time < frame["time"] <= 1 or frame["time"] < 0:
            raise ValueError("Motion keyframe times must increase from 0 to 1.")
        if not -90 <= frame["elevation"] <= 90 or frame["distance"] <= 0:
            raise ValueError("Motion elevation must be within -90 to 90 degrees, with positive distance.")
        if previous_azimuth is not None:
            travel += abs(frame["azimuth"] - previous_azimuth)
        previous_time, previous_azimuth = frame["time"], frame["azimuth"]
    if travel > 32 * 360:
        raise ValueError("A motion path exceeds the supported azimuth travel.")
    return frames


def _inverse_ease(progress):
    # The existing path samples smoothstep, 3u^2 - 2u^3. Preserve that schedule.
    lower, upper = 0., 1.
    for _ in range(52):
        middle = (lower + upper) / 2
        if middle * middle * (3 - 2 * middle) < progress:
            lower = middle
        else:
            upper = middle
    return (lower + upper) / 2


def trajectory(path_id, base_trajectory):
    selection(path_id)
    base = copy.deepcopy(base_trajectory)
    validate(base)
    if path_id == "around":
        return base
    if path_id == "diagonal":
        for frame in base:
            theta = math.radians(frame["azimuth"])
            azimuth = math.degrees(math.atan2(math.sin(theta) / math.sqrt(2), math.cos(theta)))
            if frame["azimuth"] > 180:
                azimuth += 360
            frame.update(azimuth=round(azimuth, 10),
                         elevation=round(math.degrees(math.asin(math.sin(theta) / math.sqrt(2))), 10))
        return validate(base)
    start, end = base[1]["time"], base[-1]["time"]
    result = [dict(base[0]), dict(base[1])]
    for angle in range(45, 361, 45):
        moment = end if angle == 360 else start + (end - start) * _inverse_ease(angle / 360)
        if angle in {90, 270}:
            # Azimuth is indeterminate at a pole. Two same-position poses make
            # the branch change at the pole, rather than traversing a cone.
            before, after = (0, 180) if angle == 90 else (180, 360)
            for offset, azimuth in ((-0.0000005, before), (0.0000005, after)):
                result.append({"time": moment + offset, "azimuth": azimuth,
                               "elevation": 90 if angle == 90 else -90, "distance": 1})
        else:
            elevation = angle if angle < 90 else 180 - angle if angle < 270 else angle - 360
            result.append({"time": moment, "azimuth": 0 if angle < 90 else 180 if angle < 270 else 360,
                           "elevation": elevation, "distance": 1})
    return validate(result)


def parameters(value):
    """Validate an immutable saved payload without accepting prompt/image overrides."""
    allowed = {"duration", "resolution", "seed", "enable_safety_checker", "sync_mode",
               "prompt_expansion_mode", "camera_trajectory"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("The saved motion request has unsupported parameters.")
    result = copy.deepcopy(value)
    validate(result.get("camera_trajectory"))
    if (isinstance(result.get("duration"), bool) or not isinstance(result.get("duration"), int)
            or not 5 <= result["duration"] <= 15 or result.get("resolution") not in {"480P", "768P", "1080P"}):
        raise ValueError("The saved motion request has invalid duration or resolution.")
    return result
