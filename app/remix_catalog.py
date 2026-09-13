"""Shared Reactor editing presets for the optional finished-video step."""

MODEL = "xmax/x2"
PRESETS = [
    {"id": "day-to-night", "label": "Day to night", "prompt":
     "Change only the daytime background into a realistic night scene with a dark sky, "
     "soft moonlight and practical lights. Preserve the foreground subjects, vehicles, "
     "people, their shapes, clothing and positions. Keep the original action, camera "
     "movement, framing and scene geometry. Add subtle natural night lighting. No new "
     "camera moves, zooms, cuts, text or objects."},
    {"id": "jelly-world", "label": "Jelly world", "prompt":
     "Transform the background scenery into colorful translucent jelly sculptures that "
     "gently wobble and release playful little jelly bubbles. Keep the foreground "
     "subjects and vehicles recognizable and preserve their original motion and "
     "positions. Keep the source camera movement and framing. A funny polished "
     "visual effect, with no added cuts or text."},
    {"id": "claymation", "label": "Claymation", "prompt":
     "Restyle the entire video as a detailed handmade clay animation, with sculpted "
     "clay textures and miniature scenery. Preserve every subject, vehicle, action, "
     "composition and camera move from the source. Keep the original timing. No added "
     "cuts, zooms or text."},
    {"id": "neon-racing", "label": "Neon world", "prompt":
     "Transform the background into a futuristic neon night environment with glowing "
     "light strips and subtle reflections. Preserve the foreground subjects, vehicles, "
     "people and their original action and positions. Follow the existing camera path "
     "and framing exactly. No added cuts, zooms or text."},
]


def selection(preset_id="day-to-night", prompt=None):
    preset = next((item for item in PRESETS if item["id"] == preset_id), None)
    if not preset:
        raise ValueError("Choose a Reactor effect from the list.")
    value = preset["prompt"] if prompt is None else prompt
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 1000:
        raise ValueError("Use a Reactor prompt between 1 and 1000 characters.")
    return {**preset, "prompt": value.strip()}


def public_catalog(*, credential=None):
    from app.reactor_live import SESSION_SECONDS
    configured = credential is not None
    return {"provider": "Reactor", "model": "X2", "model_id": MODEL,
            "configured": configured, "presets": PRESETS, "live_max_session_seconds": SESSION_SECONDS,
            "live_prompts": {
                "night": PRESETS[0]["prompt"],
                "day": "Show the original scene in natural daytime with a bright blue sky and daylight. "
                       "Keep the foreground subjects, vehicles, clothing, positions and actions from the source. "
                       "Preserve the source camera movement, framing and geometry. No new objects, cuts or zooms.",
            },
            "description": "Creative edits at X2's native resolution. Original audio is retained."}
