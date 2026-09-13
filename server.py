"""Local 360° World Model Clips service. Run with .venv/bin/python server.py."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import signal
import sys
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import media
import demo_auth
from runtime_paths import data_dir
import download_progress
import fal_credentials
import reactor_credentials
import orbit_paths

ROOT = Path(__file__).resolve().parent
DATA = data_dir(ROOT)
_venv_downloader = Path(sys.executable).parent / "yt-dlp"
TOOLS = Path(os.environ.get("FOOTBALL_YTDLP") or
             (_venv_downloader if _venv_downloader.is_file() else shutil.which("yt-dlp") or
              DATA / "tools" / "bin" / "yt-dlp"))
SEED = "C9sL5j_iUiE"
LOCK = threading.RLock()
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1)
GENERATION_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1)
INTERACTIVE_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1)
SEARCH_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1)
LABEL_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2)
REMIX_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2)
JOBS = {}
SEARCH_CACHE_SECONDS = 600


class LabelBusy(ValueError):
    pass


INTERNAL_CREDENTIAL = object()


def generation_status(credential=INTERNAL_CREDENTIAL):
    import fal_camera
    if credential is INTERNAL_CREDENTIAL:
        status = fal_camera.status()
    else:
        status = fal_camera.status(credential_key=credential.key if credential else None)
        status.update(authenticated=bool(credential and credential.verified),
                      credential_source="browser", checked_at=None,
                      message=("Your Fal key is connected." if credential and credential.verified else
                               "Key connected; generation access is checked on the first request." if credential else
                               "Connect your Fal API key before generating."))
    if media.read_json(DATA / "generation-settings.json", {}).get("paused"):
        status.update(ready=False, paused=True,
                      message="Fal generation is paused. Existing videos remain available to review.")
    return status


def folder(video_id):
    return DATA / "sources" / media.youtube_id(video_id)


def manifest(video_id):
    return folder(video_id) / "project.json"


def source_path(video_id):
    """Resolve the preserved original, never the browser compatibility copy."""
    location = folder(video_id)
    project = media.read_json(manifest(video_id), {})
    name = project.get("source_file")
    if name is not None:
        if name not in {"source.mp4", "source.mkv"}:
            raise ValueError("Invalid original source filename.")
        return location / name
    # Existing imported MP4s keep priority; importing never replaces them.
    return location / ("source.mp4" if (location / "source.mp4").exists() else "source.mkv")


def load_project(video_id):
    value = media.read_json(manifest(video_id))
    if not value:
        raise ValueError("Import this video first.")
    for candidate in value.get("candidates", []):
        if candidate.get("tail_seconds") != media.DEFAULTS["tail_seconds"]:
            candidate["tail_seconds"] = media.DEFAULTS["tail_seconds"]
    return value


def update_project(video_id, **changes):
    with LOCK:
        project = load_project(video_id)
        project.update(changes)
        media.write_json(manifest(video_id), project)
    return project


def public_project(video_id):
    project = load_project(video_id)
    project["audio_mode"] = audio_mode(video_id)
    details = project.get("media", {})
    if details.get("duration") and details.get("fps"):
        try:
            project["frame_selection"] = media.frame_selection_bounds(details["duration"], details["fps"])
        except ValueError:
            project["frame_selection"] = None
        for candidate in project.get("candidates", []):
            try:
                candidate["edit_window"] = media.cut_window(candidate.get("freeze_time", candidate.get("time")),
                                                          details["duration"], details["fps"])
            except (ValueError, TypeError):
                candidate["edit_window"] = None
    project.setdefault("source_quality", {"selection": "existing_unverified"})
    project.setdefault("original_url", f"/media/{video_id}/{source_path(video_id).name}")
    project.setdefault("preview_url", project.get("video_url"))
    project.setdefault("preview", {"kind": "original", "url": project.get("video_url"),
                                   "media": project.get("media"), "resolution_preserved": True,
                                   "fps_preserved": True})
    for run in project.get("generations", []):
        decorate_run_progress(video_id, run)
    return project


def decorate_run_progress(video_id, run):
    job = media.read_json(job_path(run["job_id"]), {}) if run.get("job_id") else {}
    if job.get("created_at") and not run.get("created_at"):
        run["created_at"] = job["created_at"]
    receipt = media.read_json(folder(video_id) / "exports" / run["id"] / "fal-request.json", {})
    phase = receipt.get("stage")
    if run.get("status") in {"queued", "running"}:
        run["stage"] = {"submitting": "submitting", "queued": "queued", "in_queue": "queued",
                        "in_progress": "generating", "completed": "downloading", "downloading": "downloading",
                        "normalizing": "normalizing", "complete": "anchoring"}.get(phase, "queued")
    elif run.get("status") == "complete":
        run["stage"] = ("complete" if delivery_ready(video_id, run) else
                        "assembly_failed" if run.get("composition_error") else
                        run.get("local_stage") if run.get("local_stage") in {"anchoring", "assembling"}
                        else "assembling")
    else:
        run["stage"] = run.get("status", "interrupted")
    if receipt.get("provider_timings"):
        run["provider_timings"] = receipt["provider_timings"]
    if run["stage"] == "downloading" and isinstance(receipt.get("download_progress"), dict):
        run["download_progress"] = receipt["download_progress"]
    run["stage_timings"] = {**receipt.get("stage_timings", {}), **run.get("local_stage_timings", {})}
    run["stage_started_at"] = (run.get("local_stage_started_at")
                               if run["stage"] in {"anchoring", "assembling", "complete", "assembly_failed"}
                               and run.get("local_stage_started_at") is not None
                               else receipt.get("stage_started_at"))
    return run

def job_path(job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        raise ValueError("Invalid job ID.")
    return DATA / "jobs" / f"{job_id}.json"


def save_job(job):
    with LOCK:
        value = dict(job)
        value["updated_at"] = time.time()
        if value.get("status") == "complete":
            value.pop("error", None)
        media.write_json(job_path(value["id"]), value)
        JOBS[value["id"]] = value
        return value.copy()


def read_run(video_id, run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        raise ValueError("Invalid run ID.")
    project = load_project(video_id)
    run = next((r.copy() for r in project.get("generations", []) if r["id"] == run_id), None)
    if run is None:
        raise ValueError("Unknown generation run.")
    if run.get("status") == "complete":
        run.pop("error", None)
        run.pop("error_type", None)
    return run


def delivery_ready(video_id, run):
    target = folder(video_id) / "exports" / run["id"]

    def exists(url):
        if not isinstance(url, str):
            return False
        path = urlparse(url).path
        prefix = f"/media/{video_id}/exports/{run['id']}/"
        name = path.removeprefix(prefix)
        return (path.startswith(prefix) and "/" not in name and name.endswith(".mp4")
                and (target / name).is_file())

    if run.get("status") != "complete" or not exists(run.get("url")):
        return False
    if run.get("provider") != "Fal":
        return True
    anchored = bool(run.get("delivery", {}).get("anchor_reference")
                    or run.get("preset_id") == "fal-h3-max-frozen-orbit-v2")
    # A finished registered edit remains ready after defaults change.
    return any(exists(c.get("url"))
               and (not anchored or c.get("reference_anchored") is True)
               for c in run.get("composites", []))


def run_status(video_id, run_id):
    run = read_run(video_id, run_id)
    ready = delivery_ready(video_id, run)
    status = run.get("status", "interrupted")
    error = run.get("error")
    if status == "complete" and not ready:
        bound = media.read_json(job_path(run["job_id"]), {}) if run.get("job_id") else {}
        if bound.get("status") in {"queued", "running"}:
            status = bound["status"]
        else:
            status = "failed" if run.get("composition_error") else "interrupted"
            error = run.get("composition_error") or "The orbit is saved. Its local assembly needs recovery; no generation was resubmitted."
    result = {"id": run_id, "video_id": video_id, "run_id": run_id,
              "status": status, "delivery_ready": ready, "result": run}
    if status in {"failed", "interrupted"}:
        result["error"] = error or "This run was interrupted. No generation was resubmitted."
    return result


def get_job(job_id):
    with LOCK:
        job = media.read_json(job_path(job_id)) or JOBS.get(job_id)
        if not job:
            return None
        job = job.copy()
        if job.get("video_id") and job.get("remix_id"):
            project = load_project(job["video_id"])
            remix = next((r for r in project.get("remixes", []) if r["id"] == job["remix_id"]), None)
            if remix and remix.get("status") == "complete":
                original = job.copy()
                job.update(status="complete", result=remix, finished_at=remix.get("finished_at"))
                job.pop("error", None)
                return save_job(job) if job != original else job
        if job.get("video_id") and job.get("run_id"):
            run = run_status(job["video_id"], job["run_id"])
            if run["delivery_ready"]:
                original = job.copy()
                job.update(status="complete", result=run["result"], delivery_ready=True)
                job.pop("error", None)
                return save_job(job) if job != original else job
            if job.get("status") == "complete" and run["status"] in {"failed", "interrupted"}:
                job.update(status=run["status"], result=run["result"], delivery_ready=False,
                           error=run["error"])
                return save_job(job)
        return job


def reconcile_jobs():
    """Restore tracking, continuing only acknowledged requests or local assembly."""
    recovery = []
    with LOCK:
        JOBS.clear()
        for path in (DATA / "jobs").glob("*.json"):
            try:
                job = media.read_json(path)
                if job_path(job["id"]) != path:
                    continue
                if job.get("status") in {"queued", "running"}:
                    job.update(status="interrupted", error="Server restarted. This job was not automatically repeated.")
                save_job(job)
            except (ValueError, KeyError, TypeError, OSError):
                continue
        for path in (DATA / "sources").glob("*/project.json"):
            project = media.read_json(path)
            video_id = path.parent.name
            for remix in project.get("remixes", []):
                saved = media.read_json(path.parent / "exports" / remix["id"] / "remix.json", {})
                if saved.get("id") == remix["id"] and saved.get("status") == "complete":
                    remix.update(saved)
                    remix.pop("error", None)
                elif remix.get("status") in {"queued", "running"}:
                    remix.update(status="interrupted", stage="interrupted",
                                 message="Server restarted. Start a new Reactor remix to try again.",
                                 error="The previous Reactor session was not automatically repeated.")
                    media.write_json(path.parent / "exports" / remix["id"] / "remix.json", remix)
            for run in project.get("generations", []):
                location = path.parent / "exports" / run["id"]
                recorded = media.read_json(location / "run.json", {})
                # A worker writes its run receipt before updating the project.
                if recorded.get("status") == "complete" and recorded.get("id") == run["id"]:
                    run.update(recorded)
                if run.get("status") == "complete":
                    run.pop("error", None)
                    run.pop("error_type", None)
                    if delivery_ready(video_id, run):
                        run.pop("composition_error", None)
                    elif run.get("provider") == "Fal":
                        recovery.append((video_id, run["id"]))
                elif run.get("status") in {"queued", "running"}:
                    receipt = media.read_json(location / "fal-request.json", {})
                    acknowledged = receipt.get("request_id")
                    if isinstance(acknowledged, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", acknowledged):
                        run.update(request_id=acknowledged, recovery_available=True)
                        if run.get("provider") == "Fal" and run.get("credential_source") != "browser":
                            recovery.append((video_id, run["id"]))
                    run.update(status="interrupted", error="Server restarted. The existing run was retained and no generation was resubmitted.")
                    if run.get("credential_source") == "browser":
                        run.update(requires_credential=True, error="Reconnect the same Fal key to resume this saved request.")
                media.write_json(location / "run.json", run)
            media.write_json(path, project)
        for job_id in list(JOBS):
            try:
                get_job(job_id)
            except (ValueError, KeyError):
                pass
    for video_id, run_id in recovery:
        with LOCK:
            project = load_project(video_id)
            run = next(r for r in project["generations"] if r["id"] == run_id)
            job_id = run.get("job_id") or uuid.uuid4().hex[:12]
            run.update(job_id=job_id, video_id=video_id)
            if run.get("status") != "complete":
                run.update(status="queued")
            run.pop("error", None)
            media.write_json(folder(video_id) / "exports" / run_id / "run.json", run)
            media.write_json(manifest(video_id), project)
        task("Resume saved orbit", lambda v=video_id, r=run_id: execute_generation(v, r, resume_only=True),
             job_id=job_id, video_id=video_id, run_id=run_id, executor=GENERATION_POOL)


def resume_browser_generations(credential):
    """Resume acknowledged requests only, under the original browser key owner."""
    resumed = []
    with LOCK:
        for path in (DATA / "sources").glob("*/project.json"):
            project = media.read_json(path)
            for run in project.get("generations", []):
                if (run.get("credential_source") != "browser"
                        or run.get("credential_owner") != credential.fingerprint
                        or run.get("status") != "interrupted" or not run.get("recovery_available")):
                    continue
                receipt = media.read_json(path.parent / "exports" / run["id"] / "fal-request.json", {})
                if not receipt.get("request_id"):
                    continue
                video_id, run_id = path.parent.name, run["id"]
                job_id = run.get("job_id") or uuid.uuid4().hex[:12]
                run.update(status="queued", job_id=job_id, requires_credential=False)
                run.pop("error", None)
                media.write_json(path.parent / "exports" / run_id / "run.json", run)
                media.write_json(path, project)
                resumed.append(task("Resume saved world model clip",
                    lambda v=video_id, r=run_id: execute_generation(v, r, resume_only=True, credential=credential),
                    job_id=job_id, video_id=video_id, run_id=run_id, executor=GENERATION_POOL))
    return resumed


def task(kind, operation, *, job_id=None, video_id=None, run_id=None, executor=None, metadata=None):
    job_id = job_id or uuid.uuid4().hex[:12]
    with LOCK:
        job = {"id": job_id, "kind": kind, "status": "queued", "created_at": time.time()}
        if video_id is not None:
            job["video_id"] = video_id
        if run_id is not None:
            job["run_id"] = run_id
        if metadata:
            job.update(metadata)
        save_job(job)

    def worker():
        with LOCK:
            save_job({**JOBS[job_id], "status": "running", "started_at": time.time()})
        try:
            result = operation()
            with LOCK:
                save_job({**JOBS[job_id], "status": "complete", "result": result, "finished_at": time.time()})
        except Exception as error:
            with LOCK:
                save_job({**JOBS[job_id], "status": "failed", "error": str(error)[-1800:], "finished_at": time.time()})
    (executor or POOL).submit(worker)
    return {"job_id": job_id}


def job_progress(job_id, progress):
    with LOCK:
        job = JOBS.get(job_id)
        if job and job["status"] in {"queued", "running"}:
            save_job({**job, "progress": dict(progress)})


def compact_jobs():
    with LOCK:
        jobs = sorted((get_job(job_id) for job_id in list(JOBS)),
                      key=lambda job: job.get("created_at", 0))[-30:]
        result = []
        for job in jobs:
            compact = {k: v for k, v in job.items() if k != "result"}
            if job.get("video_id") and job.get("run_id"):
                run = decorate_run_progress(job["video_id"], read_run(job["video_id"], job["run_id"]))
                compact["progress"] = {"stage": run["stage"], "stage_started_at": run.get("stage_started_at"),
                                       "stage_timings": run.get("stage_timings", {}),
                                       **run.get("download_progress", {})}
            result.append(compact)
        return result


def start_import(video_id, capabilities):
    with LOCK:
        existing = next((job for job in JOBS.values() if job.get("video_id") == video_id
                         and job.get("operation") == "import"
                         and job["status"] in {"queued", "running"}), None)
        if existing:
            return {"job_id": existing["id"]}
        job_id = uuid.uuid4().hex[:12]
        return task("Import video", lambda: import_video(video_id, playback_capabilities=capabilities,
                    on_progress=lambda value: job_progress(job_id, value)), job_id=job_id,
                    video_id=video_id, metadata={"operation": "import"})


def start_label(video_id, item_id, source_time, *, credential=None):
    import action_labels
    if isinstance(source_time, bool) or not isinstance(source_time, (int, float)) or not math.isfinite(source_time):
        raise ValueError("Choose a finite video time.")
    with LOCK:
        project = load_project(video_id)
        if not 0 <= source_time < project["media"]["duration"]:
            raise ValueError("Choose a time within the video.")
        if not any(c["id"] == item_id for c in project["candidates"]):
            raise ValueError("Unknown moment.")
        for job in JOBS.values():
            if (job.get("operation") == "label" and job.get("video_id") == video_id
                    and job["status"] in {"queued", "running"}):
                if job.get("item_id") == item_id and abs(job.get("source_time", -1) - source_time) < .000001:
                    return {"job_id": job["id"]}
                raise LabelBusy("A moment is already being identified. Playback and generation are available.")
        frame_path = None
        for run in project.get("generations", []):
            if (run.get("item_id") == item_id
                    and abs(run.get("freeze_time", -1) - source_time) < .000001):
                saved = folder(video_id) / "exports" / run["id"] / "frame.png"
                if saved.is_file() and hashlib.sha256(saved.read_bytes()).hexdigest() == run.get("frame_sha256"):
                    frame_path = saved
                    break
        if frame_path is None:
            item = next(c for c in project["candidates"] if c["id"] == item_id)
            saved = folder(video_id) / "frames" / f"{item_id}.png"
            if (item.get("frame_url") and saved.is_file()
                    and abs(item.get("freeze_time", -1) - source_time) < .000001):
                image_bytes = saved.read_bytes()
                frame_path = folder(video_id) / "action-labels" / "frames" / (hashlib.sha256(image_bytes).hexdigest() + ".png")
                frame_path.parent.mkdir(parents=True, exist_ok=True)
                if not frame_path.exists():
                    frame_path.write_bytes(image_bytes)
        enqueued = time.monotonic()
        def operation():
            remaining = action_labels.TIMEOUT_SECONDS - (time.monotonic() - enqueued)
            if remaining <= 0:
                result = {"status": "timeout", "source_time": source_time, "label": "Action not identified",
                          "confidence": "low", "request_submitted": False}
            else:
                result = action_labels.label_moment(source_path(video_id), source_time,
                    folder(video_id) / "action-labels", frame_path=frame_path, timeout=remaining,
                    **({"credential_key": credential.key} if credential else {}))
            with LOCK:
                current = load_project(video_id)
                item = next((c for c in current["candidates"] if c["id"] == item_id), None)
                tolerance = 1 / max(1, float(current["media"]["fps"]))
                if item and abs(item.get("freeze_time", item.get("time", -1)) - source_time) <= tolerance:
                    item["action_label"] = result
                for run in current.get("generations", []):
                    if run.get("item_id") == item_id and abs(run.get("freeze_time", -1) - source_time) <= tolerance:
                        run["action_label"] = result
                        media.write_json(folder(video_id) / "exports" / run["id"] / "run.json", run)
                media.write_json(manifest(video_id), current)
            return {"video_id": video_id, "item_id": item_id, "time": source_time, "action_label": result}
        return task("Identify action", operation, video_id=video_id, executor=LABEL_POOL,
                    metadata={"operation": "label", "item_id": item_id, "source_time": source_time})


def start_assembly(video_id, run_id, tail):
    with LOCK:
        existing = next((job for job in JOBS.values() if job.get("video_id") == video_id
                         and job.get("operation") == "assemble" and job.get("reference_run_id") == run_id
                         and job["status"] in {"queued", "running"}), None)
        if existing:
            return {"job_id": existing["id"]}
        job_id = uuid.uuid4().hex[:12]
        return task("Assemble highlight", lambda: assemble_highlight(video_id, run_id, tail,
                    on_progress=lambda stage: job_progress(job_id, {"stage": stage})),
                    job_id=job_id, video_id=video_id, executor=GENERATION_POOL,
                    metadata={"operation": "assemble", "reference_run_id": run_id})


def prepare_source_project(video_id):
    location = folder(video_id)
    source = source_path(video_id)
    details = media.probe(source)
    # Decode picture and sound at both ends, separately from metadata discovery.
    media.verify_source_decode(source, details)
    info = media.read_json(location / "source.info.json", {})
    details.update(decoded_start=True, decoded_end=True)
    media.write_json(location / "verification.json", details)
    old = media.read_json(manifest(video_id), {})
    old.update(id=video_id, title=info.get("title", old.get("title", video_id)),
               channel=info.get("channel", old.get("channel", "YouTube")),
               source_url=f"https://www.youtube.com/watch?v={video_id}", media=details,
               source_file=source.name, original_url=f"/media/{video_id}/{source.name}",
               source_quality=media.read_json(location / "source-quality.json", {"selection": "existing_unverified"}))
    old.setdefault("candidates", [])
    old.setdefault("waveform", [])
    old.setdefault("exports", [])
    if not old.get("video_url"):
        old.update(status="preparing_preview", preview_url=None, video_url=None)
    media.write_json(manifest(video_id), old)
    return old


def playback_capability_options(value):
    if value is None:
        return {"av1_mp4": False}
    if not isinstance(value, dict) or not isinstance(value.get("av1_mp4", False), bool):
        raise ValueError("Browser playback capabilities must contain a boolean av1_mp4 value.")
    return {"av1_mp4": value.get("av1_mp4", False)}


def verify_source(video_id, *, playback_capabilities=None, on_progress=None):
    capabilities = playback_capability_options(playback_capabilities)
    if on_progress:
        on_progress({"stage": "verifying_source"})
    project = prepare_source_project(video_id)
    location = folder(video_id)
    source = source_path(video_id)
    details = project["media"]
    if on_progress:
        on_progress({"stage": "preparing_preview"})
    preview = media.browser_preview(source, location, details, **capabilities)
    preview["url"] = f"/media/{video_id}/{Path(preview.pop('path')).name}"
    return update_project(video_id, preview=preview, preview_url=preview["url"],
                          video_url=preview["url"], status="ready")


def football_title(title):
    return isinstance(title, str) and re.search(r"\bfootball\b", title, re.IGNORECASE) is not None


def manual_project(video_id):
    """Expose the whole source without replacing a saved user selection."""
    with LOCK:
        project = load_project(video_id)
        duration, fps = project["media"]["duration"], project["media"]["fps"]
        lead, tail = media.DEFAULTS["lead_seconds"], media.DEFAULTS["tail_seconds"]
        try:
            bounds = media.frame_selection_bounds(duration, fps)
            freeze_time, available = bounds["min_time"], True
        except ValueError:
            freeze_time, available = lead, False
        found = list(project.get("candidates", []))
        if available and not any(item.get("kind") == "manual" or item.get("id") == "manual" for item in found):
            found.append({"id": "manual", "kind": "manual", "time": freeze_time,
                          "freeze_time": freeze_time, "tail_seconds": tail,
                          "window_start": 0, "window_end": duration, "label": "Full video",
                          "cue": "Scrub the full video and save the frame you want to orbit.",
                          "signals": [], "selected": False})
        old_analysis = project.get("analysis")
        if old_analysis and old_analysis.get("mode") != "manual":
            project.setdefault("previous_analysis", old_analysis)
        project.update(selection_mode="manual", candidates=found, analysis={
            "mode": "manual", "reason": "title_does_not_contain_football",
            "caption_download_skipped": True, "audio_analysis_skipped": True,
            "generation_available": available, "crowd_classifier": False})
        media.write_json(manifest(video_id), project)
        return project


def analyze(video_id, *, on_progress=None):
    project = load_project(video_id)
    if not football_title(project.get("title")):
        if on_progress:
            on_progress({"stage": "manual_selection"})
        return manual_project(video_id)
    location = folder(video_id)
    if on_progress:
        on_progress({"stage": "analyzing"})
    caption_file = location / "captions.en-orig.vtt"
    if not caption_file.exists():
        caption_file = location / "captions.en.vtt"
    transcript = media.captions(caption_file)
    speech = media.commentary_events(transcript)
    audio, waveform = (media.audio_activity(source_path(video_id), location)
                       if project["media"].get("audio_present", True) else ([], []))
    found = media.candidates(speech + audio, project["media"]["duration"])
    # Preserve user selections when analysis is repeated.
    media.write_json(location / "transcript.json", transcript)
    with LOCK:
        previous = {item["id"]: item for item in load_project(video_id)["candidates"]}
        for item in found:
            old = previous.get(item["id"], {})
            for key in ("selected", "freeze_time", "tail_seconds", "frame_url", "action_label"):
                if key in old:
                    item[key] = old[key]
        return update_project(video_id, candidates=found, waveform=waveform, selection_mode="suggested",
                              analysis={"mode": "football", "caption_entries": len(transcript), "commentary_cues": len(speech),
                                        "audio_peaks": len(audio), "crowd_classifier": False})


def downloader_base():
    return [TOOLS, "--ignore-config", "--js-runtimes", "node", "--no-playlist",
            "--socket-timeout", "20", "--retries", "2"]


def download_original(video_id, *, on_progress=None):
    location = folder(video_id)
    location.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    base = downloader_base()
    if not source_path(video_id).is_file():
        if any((location / name).exists() for name in ("source.mp4", "source.mkv")):
            raise RuntimeError("An existing original needs its saved filename repaired before import; no source was replaced.")
        pending = location / "import-original"
        pending.mkdir(exist_ok=True)
        downloaded = pending / "source.mkv"
        saved_info = pending / "source.info.json"
        if downloaded.is_file() and not saved_info.is_file():
            raise RuntimeError("The downloaded original is preserved but its selection metadata is missing; no replacement was downloaded.")
        if not downloaded.is_file():
            download_progress.run(base + ["--write-info-json", "--abort-on-error",
                             "--abort-on-unavailable-fragments", "--merge-output-format", "mkv",
                             "--remux-video", "mkv", "--format", media.SOURCE_FORMAT,
                             "--format-sort-force", "--format-sort", media.SOURCE_SORT,
                             "--output", pending / "source.%(ext)s", url],
                                  on_progress=on_progress, timeout=7200)
        if on_progress:
            on_progress({"stage": "verifying_download"})
        details = media.probe(downloaded)
        media.verify_source_decode(downloaded, details)
        info = media.read_json(saved_info, {})
        frame_rate_check = media.validate_source_selection(info, details)
        downloaded.replace(location / "source.mkv")
        saved_info.replace(location / "source.info.json")
        media.write_json(location / "source-quality.json", {
            "selection": "best_available", "format": media.SOURCE_FORMAT, "sort": media.SOURCE_SORT,
            "format_id": info.get("format_id"), "imported_at": time.time(),
            "frame_rate_check": frame_rate_check,
            "original_preserved": True,
            "scope": "Highest resolution, then frame rate, among formats available to yt-dlp at import time."})
    return source_path(video_id)


def import_video(video_id, *, playback_capabilities=None, on_progress=None):
    capabilities = playback_capability_options(playback_capabilities)
    started = time.monotonic()

    def progress(value):
        if on_progress:
            on_progress({**value, "elapsed_seconds": round(time.monotonic() - started, 3)})

    progress({"stage": "checking_source"})
    download_original(video_id, on_progress=progress)
    location = folder(video_id)
    url = f"https://www.youtube.com/watch?v={video_id}"
    base = downloader_base()
    info = media.read_json(location / "source.info.json", {})
    prior = media.read_json(manifest(video_id), {})
    title = info.get("title", prior.get("title", video_id))
    if football_title(title) and not list(location.glob("captions*.vtt")):
        progress({"stage": "fetching_captions"})
        try:
            media.run(base + ["--skip-download", "--write-auto-subs", "--write-subs", "--sub-langs",
                             "en,en-orig", "--sub-format", "vtt", "--output", location / "captions.%(ext)s", url])
        except RuntimeError:
            pass  # Analysis explicitly reports zero captions and uses audio peaks.
    verify_source(video_id, playback_capabilities=capabilities, on_progress=progress)
    result = analyze(video_id, on_progress=progress)
    progress({"stage": "complete"})
    return result


def search(query):
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 180:
        raise ValueError("Enter a game, teams, or a YouTube link.")
    query = query.strip()
    if query.startswith("https://") or media.VIDEO_ID.fullmatch(query):
        video_id = media.youtube_id(query)
        target = f"https://www.youtube.com/watch?v={video_id}"
        # A downloaded video's metadata is enough to reopen it by its exact link.
        if source_path(video_id).is_file():
            info = media.read_json(folder(video_id) / "source.info.json", {})
            if info.get("title") and info.get("id", video_id) == video_id:
                return [{"id": video_id, "title": info["title"],
                         "channel": info.get("channel", info.get("uploader", "YouTube")),
                         "duration": info.get("duration"),
                         "thumbnail": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"}]
    else:
        target = "ytsearch8:" + query
    cached_path = DATA / "search-cache" / (hashlib.sha256(target.encode()).hexdigest() + ".json")
    try:
        cached = media.read_json(cached_path, {})
        age = time.time() - cached.get("fetched_at", 0)
        results = cached.get("results")
        if (cached.get("target") == target and 0 <= age < SEARCH_CACHE_SECONDS
                and isinstance(results, list) and 0 < len(results) <= 8
                and all(isinstance(item, dict) and isinstance(item.get("id"), str)
                        and media.VIDEO_ID.fullmatch(item["id"]) and isinstance(item.get("title"), str)
                        for item in results)):
            return results
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    raw = media.run([TOOLS, "--ignore-config", "--js-runtimes", "node", "--flat-playlist",
                     "--dump-json", "--skip-download", "--socket-timeout", "15", target], timeout=90)
    results = []
    for line in raw.decode().splitlines():
        value = json.loads(line)
        video_id = value.get("id", "")
        if media.VIDEO_ID.fullmatch(video_id):
            results.append({"id": video_id, "title": value.get("title", video_id),
                            "channel": value.get("channel", value.get("uploader", "YouTube")),
                            "duration": value.get("duration"),
                            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"})
    if results:
        try:
            media.write_json(cached_path, {"target": target, "fetched_at": time.time(), "results": results})
        except OSError:
            pass
    return results


def selection(video_id, item_id, changes):
    with LOCK:
        project = load_project(video_id)
        item = next((c for c in project["candidates"] if c["id"] == item_id), None)
        if item is None:
            raise ValueError("Unknown cut-up.")
        if "selected" in changes:
            if not isinstance(changes["selected"], bool):
                raise ValueError("Selection must be true or false.")
            item["selected"] = changes["selected"]
        if "freeze_time" in changes:
            window = media.cut_window(float(changes["freeze_time"]), project["media"]["duration"],
                                      project["media"]["fps"], float(changes.get("tail_seconds", media.DEFAULTS["tail_seconds"])))
            if item.get("freeze_time") != window["freeze_time"]:
                item.pop("frame_url", None)
            item.update(freeze_time=window["freeze_time"], tail_seconds=window["tail_seconds"])
        media.write_json(manifest(video_id), project)
    return item


def frame(video_id, item_id, time, tail):
    project = load_project(video_id)
    if not any(c["id"] == item_id for c in project["candidates"]):
        raise ValueError("Unknown cut-up.")
    window = media.cut_window(time, project["media"]["duration"], project["media"]["fps"], tail)
    target = folder(video_id) / "frames" / f"{item_id}.png"
    target.parent.mkdir(exist_ok=True)
    pending = target.with_name(f"{item_id}-{uuid.uuid4().hex[:8]}.png")
    media.extract_frame(source_path(video_id), window["freeze_time"], pending)
    url = f"/media/{video_id}/frames/{item_id}.png?v={uuid.uuid4().hex[:8]}"
    with LOCK:
        project = load_project(video_id)
        pending.replace(target)
        next(c for c in project["candidates"] if c["id"] == item_id).update(
            frame_url=url, freeze_time=window["freeze_time"], tail_seconds=tail)
        media.write_json(manifest(video_id), project)
    return {"frame_url": url, "freeze_time": window["freeze_time"]}


def export_sources(video_id, combined):
    project = load_project(video_id)
    selected = [c.copy() for c in project["candidates"] if c["selected"]]
    if not selected:
        raise ValueError("Select at least one cut-up.")
    export_id = uuid.uuid4().hex[:12]
    target = folder(video_id) / "exports" / export_id
    target.mkdir(parents=True)
    receipts = []
    for index, item in enumerate(selected, 1):
        window = media.cut_window(item.get("freeze_time", item["time"]), project["media"]["duration"],
                                  project["media"]["fps"], item.get("tail_seconds", media.DEFAULTS["tail_seconds"]))
        name = f"{index:02d}-{item['id']}"
        image = target / f"{name}.png"
        media.extract_frame(source_path(video_id), window["freeze_time"], image)
        media.source_cut(source_path(video_id), window, target / f"{name}.mp4")
        # The request is complete but must receive the uploaded frame's public URL before submission.
        request = media.orbit_request("UPLOAD_SELECTED_FRAME_AND_REPLACE_THIS_VALUE")
        media.write_json(target / f"{name}.request.json", request)
        receipts.append({"name": name, "source_id": video_id, "source_url": project["source_url"],
                         "window": window, "frame_file": image.name,
                         "request_file": f"{name}.request.json", "orbit_generated": False})
    media.write_json(target / "manifest.json", {"kind": "source-cutups-and-orbit-requests",
                                               "clips": receipts, "generation": generation_status()})
    if combined:
        playlist = target / "concat.txt"
        playlist.write_text("".join(f"file '{r['name']}.mp4'\n" for r in receipts))
        media.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "1", "-i", playlist,
                   "-c", "copy", "-movflags", "+faststart", target / "source-reel.mp4"])
    with zipfile.ZipFile(target / "cutups.zip", "w", compression=zipfile.ZIP_STORED) as archive:
        for path in target.iterdir():
            if path.suffix in {".mp4", ".png", ".json"}:
                archive.write(path, path.name)
    result = {"id": export_id, "kind": "Source cut-ups + orbit requests", "count": len(receipts),
              "url": f"/media/{video_id}/exports/{export_id}/cutups.zip",
              "reel_url": f"/media/{video_id}/exports/{export_id}/source-reel.mp4" if combined else None}
    with LOCK:
        current = load_project(video_id)
        current["exports"].insert(0, result)
        media.write_json(manifest(video_id), current)
    return result


def start_generation(video_id, mode, reference_run_id=None, *, item_id=None, credential=None, orbit_path=None):
    if orbit_path is not None:
        orbit_paths.selection(orbit_path)
    if mode != "fal-h3-max":
        raise ValueError("Choose Fal H3 Max camera controls.")
    connection = generation_status(credential) if credential else generation_status()
    if connection.get("paused"):
        raise ValueError("Fal generation is paused. No request was submitted.")
    if not connection["ready"]:
        raise ValueError("Configure the server's Fal key before generating.")
    with LOCK:
        project = load_project(video_id)
        if reference_run_id is not None:
            previous = read_run(video_id, reference_run_id)
            if orbit_path is None:
                orbit_path = previous.get("orbit_path", orbit_paths.DEFAULT)
            if previous.get("provider") != "Fal" or previous.get("status") != "complete":
                raise ValueError("Choose a completed Fal run to reuse its frame.")
            selected_frame = folder(video_id) / "exports" / reference_run_id / "frame.png"
            if not selected_frame.is_file() or hashlib.sha256(selected_frame.read_bytes()).hexdigest() != previous.get("frame_sha256"):
                raise ValueError("The original run's saved frame could not be verified.")
            item = {"id": previous["item_id"], "freeze_time": previous["freeze_time"],
                    "tail_seconds": media.DEFAULTS["tail_seconds"]}
        else:
            if item_id is not None:
                item = next((c.copy() for c in project["candidates"]
                             if isinstance(item_id, str) and c["id"] == item_id), None)
                if item is None:
                    raise ValueError("Choose an existing cut-up for this orbit generation.")
            else:
                selected = [c.copy() for c in project["candidates"] if c.get("selected")]
                if len(selected) != 1:
                    raise ValueError("Keep exactly one cut-up for this orbit generation.")
                item = selected[0]
            if "freeze_time" not in item or not item.get("frame_url"):
                raise ValueError("Set a freeze frame on the selected cut-up first.")
            selected_frame = folder(video_id) / "frames" / f"{item['id']}.png"
        chosen_path = orbit_paths.selection(orbit_path or orbit_paths.DEFAULT)
        request = media.orbit_request("SAVED_FRAME", chosen_path["id"])
        edit_window = media.cut_window(item["freeze_time"], project["media"]["duration"], project["media"]["fps"])
        if not selected_frame.is_file():
            raise ValueError("The saved frame file is missing. Set the freeze frame again.")
        runs = project.setdefault("generations", [])
        if any(r["status"] in {"queued", "running"} for r in runs):
            raise ValueError("An orbit test is already running. Wait for its result.")
        run_id = uuid.uuid4().hex[:12]
        job_id = uuid.uuid4().hex[:12]
        target = folder(video_id) / "exports" / run_id
        target.mkdir(parents=True)
        frame_path = target / "frame.png"
        shutil.copyfile(selected_frame, frame_path)
        prompt = request["prompt"]
        record = {"id": run_id, "job_id": job_id, "video_id": video_id,
                  "status": "queued", "created_at": time.time(), "kind": "World model clip",
                  "model": connection["model"], "provider": "Fal", "item_id": item["id"],
                  "freeze_time": item["freeze_time"], "tail_seconds": item.get("tail_seconds", media.DEFAULTS["tail_seconds"]),
                  "lead_seconds": media.DEFAULTS["lead_seconds"], "edit_window": edit_window,
                  "audio_mode": audio_mode(video_id),
                  "frame_sha256": hashlib.sha256(frame_path.read_bytes()).hexdigest(),
                  "preset_id": media.ORBIT_PRESET["id"],
                  "orbit_path": chosen_path["id"], "orbit_path_label": chosen_path["label"],
                  "orbit_path_version": orbit_paths.VERSION,
                  "request_parameters": {k: v for k, v in request.items() if k not in {"image_url", "prompt"}},
                  "delivery": dict(media.ORBIT_PRESET.get("delivery", {})),
                  "prompt": prompt, "exact_camera_controls": True}
        if credential:
            record.update(credential_source="browser", credential_owner=credential.fingerprint)
        if reference_run_id is not None:
            record["reference_run_id"] = reference_run_id
        label = item.get("action_label", {})
        if abs(label.get("source_time", -1) - item["freeze_time"]) <= 1 / max(1, float(project["media"]["fps"])):
            record["action_label"] = label
        runs.insert(0, record)
        media.write_json(target / "run.json", record)
        media.write_json(manifest(video_id), project)

    result = task("Generate with world models", lambda: execute_generation(video_id, run_id, **({"credential": credential} if credential else {})),
                  job_id=job_id, video_id=video_id, run_id=run_id, executor=GENERATION_POOL)
    result.update(run_id=run_id, video_id=video_id)
    return result


def execute_generation(video_id, run_id, *, resume_only=False, credential=None):
    snapshot = read_run(video_id, run_id)
    target = folder(video_id) / "exports" / run_id
    frame_path = target / "frame.png"
    prompt = snapshot["prompt"]

    def request_options():
        if snapshot.get("request_parameters") is not None:
            return {"request_parameters": snapshot["request_parameters"], "orbit_path": snapshot.get("orbit_path", orbit_paths.DEFAULT)}
        return {}

    def credential_options():
        if snapshot.get("credential_source") == "browser":
            if not credential or credential.fingerprint != snapshot.get("credential_owner"):
                raise fal_credentials.CredentialError("Reconnect the same Fal key to resume this saved request.")
            return {"credential_key": credential.key}
        if credential:
            raise fal_credentials.CredentialError("This saved request belongs to a different credential owner.")
        return {}

    def save(**changes):
        with LOCK:
            current = load_project(video_id)
            run = next(r for r in current["generations"] if r["id"] == run_id)
            run.update(changes)
            if run.get("status") == "complete":
                run.pop("error", None)
                run.pop("error_type", None)
            media.write_json(target / "run.json", run)
            media.write_json(manifest(video_id), current)
            return run.copy()

    def local_progress(stage):
        current = read_run(video_id, run_id)
        previous, started = current.get("local_stage"), current.get("local_stage_started_at")
        if previous == stage:
            return
        now = time.time()
        timings = dict(current.get("local_stage_timings", {}))
        if previous in {"anchoring", "assembling"} and isinstance(started, (int, float)):
            timings[previous] = round(timings.get(previous, 0) + max(0, now - started), 3)
        save(local_stage=stage, local_stage_started_at=now, local_stage_timings=timings)

    def generate():
        if resume_only and snapshot.get("status") == "complete":
            try:
                assemble_highlight(video_id, run_id, on_progress=local_progress)
                local_progress("complete")
                return read_run(video_id, run_id)
            except Exception:
                return save(composition_error="The orbit is saved. Retry local assembly to finish the highlight.")
        try:
            import fal_camera
            save(status="running")
            if resume_only:
                receipt = media.read_json(target / "fal-request.json", {})
                if not receipt.get("request_id"):
                    raise ValueError("The saved run has no acknowledged request to resume.")
                if receipt.get("stage") in {"completed", "complete", "downloading", "normalizing"} and any(
                        (target / name).is_file() for name in ("fal-camera-native.mp4", "fal-camera.mp4")):
                    result = fal_camera.recover_local(target)
                else:
                    result = fal_camera.generate(frame_path, target, prompt, seconds=receipt["seconds"], resume_only=True, **credential_options(), **request_options())
            else:
                result = fal_camera.generate(frame_path, target, prompt, seconds=snapshot.get("request_parameters", {}).get("duration", media.DEFAULTS["orbit_seconds"]),
                                             **credential_options(), **request_options())
            output = Path(result["path"]).resolve()
            if not output.is_relative_to(target.resolve()) or output.suffix != ".mp4":
                raise ValueError("The generator did not produce a local MP4 in this run.")
            details = result["media"]
            media.run(["ffmpeg", "-v", "error", "-xerror", "-i", output, "-f", "null", "-"])
            saved = save(status="complete", url=f"/media/{video_id}/exports/{run_id}/{output.name}",
                        media=details, session_id=result.get("session_id"), clip_id=result.get("clip_id"),
                        request_id=result.get("request_id"),
                        native_media=result.get("native_media"), normalization=result.get("normalization"),
                        capture_method=result.get("capture_method", "provider_video"), audio=result.get("audio"))
        except Exception as error:
            # Provider internals may contain signed URLs. Keep public errors generic.
            detail = str(error)
            try:
                detail = detail.replace(credential.key if credential else
                    fal_camera.api_key(None if snapshot.get("credential_source") == "browser" else fal_camera.ENV_CREDENTIAL), "[credential]")
            except Exception:
                pass
            detail = re.sub(r"https?://\S+|rk_[A-Za-z0-9]+|eyJ[A-Za-z0-9_.-]+", "[redacted]", detail)
            media.write_json(target / "diagnostic.json", {"type": type(error).__name__, "detail": detail[-1600:]})
            message = (str(error) if isinstance(error, (fal_camera.FalResolutionError, fal_credentials.CredentialError)) else
                       "Fal did not complete this orbit. No automatic resubmission was made.")
            save(status="failed", error=message)
            raise RuntimeError(message) from None
        try:
            assemble_highlight(video_id, run_id, on_progress=local_progress)
            local_progress("complete")
            return next(r for r in load_project(video_id)["generations"] if r["id"] == run_id)
        except Exception:
            return save(composition_error="The orbit is saved. Retry local assembly to finish the highlight.")

    return generate()


def delivery_size(project):
    source = project.get("media", {})
    return (3840, 2160) if source.get("width", 0) >= 3840 and source.get("height", 0) >= 2160 else (1920, 1080)


def audio_mode(video_id):
    # Every new edit uses this video's own complete soundtrack.
    return "source_slow_motion"


def reference_delivery(video_id, run_id):
    project = load_project(video_id)
    run = next(r.copy() for r in project.get("generations", []) if r["id"] == run_id)
    required = run.get("delivery", {}).get("anchor_reference") or run.get("preset_id") == "fal-h3-max-frozen-orbit-v2"
    if not required:
        return run
    target = folder(video_id) / "exports" / run_id
    current_path = target / Path(urlparse(run["url"]).path).name
    transition = run.get("delivery", {}).get("transition", "blend")
    current_closure = run.get("reference_closure", {})
    output_size = delivery_size(project)
    delivery_dimensions_match = (output_size != (3840, 2160)
                                or (run.get("media", {}).get("width"), run.get("media", {}).get("height")) == output_size)
    if (current_closure.get("first_last_pixel_hash_equal") and current_path.is_file()
            and current_closure.get("transition", "blend") == transition and delivery_dimensions_match):
        return run
    import orbit_loop
    output = target / f"fal-camera-loop-{uuid.uuid4().hex[:8]}.mp4"
    options = {"transition": transition} if transition != "blend" else {}
    if output_size == (3840, 2160):
        options["output_size"] = output_size
    closure = orbit_loop.anchor_reference(target / "fal-camera.mp4", target / "frame.png", output, **options)
    closure.pop("path", None)
    media.write_json(output.with_suffix(".json"), closure)
    with LOCK:
        current = load_project(video_id)
        saved = next(r for r in current["generations"] if r["id"] == run_id)
        saved.update(raw_url=f"/media/{video_id}/exports/{run_id}/fal-camera.mp4",
                     url=f"/media/{video_id}/exports/{run_id}/{output.name}",
                     reference_closure=closure, media=closure["media"])
        media.write_json(target / "run.json", saved)
        media.write_json(manifest(video_id), current)
        return saved.copy()


def assemble_highlight(video_id, run_id, tail=None, *, on_progress=None):
    import composite
    import source_atmosphere
    if tail is None:
        tail = media.DEFAULTS["tail_seconds"]
    if isinstance(tail, bool) or tail != media.DEFAULTS["tail_seconds"]:
        raise ValueError(f"Choose {media.DEFAULTS['tail_seconds']} seconds of continued play.")
    tail = int(tail)
    project = load_project(video_id)
    run = next((r.copy() for r in project.get("generations", []) if r["id"] == run_id), None)
    if not run or run.get("provider") != "Fal" or run["status"] != "complete":
        raise ValueError("Choose a completed Fal orbit.")
    if on_progress is not None:
        on_progress("anchoring")
    run = reference_delivery(video_id, run_id)
    anchored = run.get("reference_closure", {}).get("first_last_pixel_hash_equal", False)
    transition = run.get("reference_closure", {}).get("transition", "blend") if anchored else "none"
    resume_next_frame = run.get("delivery", {}).get("resume_next_frame", False)
    join_version = "cut_next_frame_v1" if transition == "cut" and resume_next_frame else "legacy_v1"
    target = folder(video_id) / "exports" / run_id
    audio_version = source_atmosphere.METHOD
    suffix = "-matched" if anchored else ""
    suffix += f"-{audio_version}"
    if join_version != "legacy_v1":
        suffix += f"-{join_version}"
    lead = media.DEFAULTS["lead_seconds"]
    seconds = media.DEFAULTS["orbit_seconds"]
    output_size = delivery_size(project)
    if output_size == (3840, 2160):
        suffix += "-4k"
    output = target / f"highlight-{lead}-{seconds}-{tail}{suffix}.mp4"
    existing = next((c for c in run.get("composites", []) if c["tail_seconds"] == tail
                     and c.get("lead_seconds") == lead and c.get("audio_version") == audio_version
                     and c.get("join_version", "legacy_v1") == join_version
                     and (output_size != (3840, 2160)
                          or (c.get("media", {}).get("width"), c.get("media", {}).get("height")) == output_size)), None)
    if existing and existing.get("reference_anchored", False) == anchored and output.is_file():
        return existing
    pending = target / f"highlight-{uuid.uuid4().hex[:8]}.mp4"
    try:
        if on_progress is not None:
            on_progress("assembling")
        options = {"reference_anchored": True} if anchored else {}
        if output_size == (3840, 2160):
            options["output_size"] = output_size
        if resume_next_frame:
            options["resume_next_frame"] = True
        source_window = media.cut_window(run["freeze_time"], project["media"]["duration"],
                                         project["media"]["fps"], tail, resume_next_frame=resume_next_frame)
        bed_seconds = seconds + composite.JOIN_FADE_SECONDS + min(composite.JOIN_FADE_SECONDS,
                                                                  source_window["actual_tail_seconds"])
        bed = source_atmosphere.prepare_slow_source_bed(source_path(video_id), run["freeze_time"],
                                                      target, seconds=bed_seconds)
        options.update(crowd_path=bed["path"], crowd_provenance=bed["provenance"], auto_crowd=False)
        receipt = composite.assemble(source_path(video_id), target / Path(urlparse(run["url"]).path).name,
                                     run["freeze_time"], pending, tail_seconds=tail, **options)
        receipt.pop("path", None)
        receipt["audio_provenance"].update(orbit="source_slow_motion_loop", audio_version=audio_version,
                                            source_audio=bed["provenance"])
        receipt["audio_provenance"].pop("crowd_bed", None)
        native = run.get("native_media", {})
        receipt.setdefault("resolution_provenance", {}).update(
            source_size=[project.get("media", {}).get("width"), project.get("media", {}).get("height")],
            generated_size=[native.get("width"), native.get("height")],
            generated_orbit_upscaled=bool(native.get("width") and native["width"] < output_size[0]),
            reference_size=run.get("reference_closure", {}).get("reference_render"))
        pending.replace(output)
        result = {"id": f"{run_id}-{lead}-{tail}{suffix}", "run_id": run_id, "tail_seconds": tail,
                  "lead_seconds": lead, "audio_version": audio_version,
                  "actual_tail_seconds": receipt.get("source_window", {}).get("actual_tail_seconds", tail),
                  "join_version": join_version, "transition": transition,
                  "reference_anchored": anchored,
                  "url": f"/media/{video_id}/exports/{run_id}/{output.name}", **receipt}
        previous = next((c for c in reversed(run.get("composites", []))
                         if c.get("tail_seconds") == tail and c.get("url") != result["url"]), None)
        if previous:
            result["previous_edit_url"] = previous["url"]
        media.write_json(output.with_suffix(".json"), result)
        with LOCK:
            current = load_project(video_id)
            saved = next(r for r in current["generations"] if r["id"] == run_id)
            saved["composites"] = [c for c in saved.get("composites", [])
                                   if (c["tail_seconds"] != tail or c.get("lead_seconds") != lead
                                       or c.get("audio_version") != audio_version
                                       or (c.get("media", {}).get("width", 1920), c.get("media", {}).get("height", 1080)) != output_size)] + [result]
            saved.pop("composition_error", None)
            media.write_json(target / "run.json", saved)
            media.write_json(manifest(video_id), current)
        return result
    finally:
        pending.unlink(missing_ok=True)


def combine_highlights(video_id, items):
    import composite
    if not isinstance(items, list) or not 1 <= len(items) <= 40:
        raise ValueError("Choose 1 to 40 completed highlights.")
    project = load_project(video_id)
    available = {c["id"]: c for r in project.get("generations", [])
                 if r.get("provider") == "Fal" and r.get("status") == "complete"
                 for c in r.get("composites", [])}
    if any(not isinstance(item, str) or item not in available for item in items):
        raise ValueError("Choose completed outputs from this video.")
    if len(set(items)) != len(items):
        raise ValueError("Choose each output once.")
    chosen = [available[item] for item in items]
    paths = [folder(video_id) / "exports" / c["run_id"] / Path(urlparse(c["url"]).path).name for c in chosen]
    if any(not path.is_file() for path in paths):
        raise ValueError("A selected finished output is missing. Assemble it again before combining.")
    existing = next((r for r in project.get("reels", []) if r.get("outputs") == items
                     and (folder(video_id) / "exports" / r["id"] / "highlights-reel.mp4").is_file()), None)
    if existing:
        return existing
    export_id = uuid.uuid4().hex[:12]
    target = folder(video_id) / "exports" / export_id
    target.mkdir(parents=True)
    output = target / "highlights-reel.mp4"
    result = composite.combine(paths, output)
    result.pop("path", None)
    result.pop("clips", None)
    result.update(id=export_id, outputs=items, url=f"/media/{video_id}/exports/{export_id}/{output.name}")
    media.write_json(target / "reel.json", result)
    with LOCK:
        current = load_project(video_id)
        current.setdefault("reels", []).insert(0, result)
        media.write_json(manifest(video_id), current)
    return result


def remix_source(video_id, composite_id):
    """Resolve only a completed, locally owned highlight as the Reactor input."""
    video_id = media.youtube_id(video_id)
    if not isinstance(composite_id, str):
        raise ValueError("Choose a finished clip for Reactor.")
    project = load_project(video_id)
    selected = next(((c, r["id"]) for r in project.get("generations", [])
                   if r.get("provider") == "Fal" and r.get("status") == "complete"
                   for c in r.get("composites", []) if c.get("id") == composite_id), None)
    if not selected:
        raise ValueError("Generate and finish the original clip before using Reactor.")
    chosen, owner = selected
    run_id = chosen.get("run_id", "")
    if (not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id)
            or run_id != owner):
        raise ValueError("Invalid finished clip location.")
    parsed = urlparse(chosen.get("url", ""))
    prefix = f"/media/{video_id}/exports/{run_id}/"
    filename = parsed.path.removeprefix(prefix)
    if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
            or not parsed.path.startswith(prefix) or Path(filename).name != filename
            or not filename.endswith(".mp4")):
        raise ValueError("Invalid finished clip location.")
    directory = folder(video_id) / "exports" / run_id
    source = directory / filename
    if (not source.is_file() or source.is_symlink() or directory.is_symlink()
            or (folder(video_id) / "exports").is_symlink()
            or not source.resolve().is_relative_to((folder(video_id) / "exports").resolve())):
        raise ValueError("The finished clip is missing or outside this project.")
    return source, chosen


def save_remix(video_id, remix_id, **changes):
    with LOCK:
        project = load_project(video_id)
        result = next(r for r in project.get("remixes", []) if r["id"] == remix_id)
        result.update(changes)
        media.write_json(folder(video_id) / "exports" / remix_id / "remix.json", result)
        media.write_json(manifest(video_id), project)
        return result.copy()


def start_remix(video_id, composite_id, preset_id="day-to-night", prompt=None, *, credential=None):
    import remix_catalog
    if credential is None:
        raise reactor_credentials.CredentialError("Connect your Reactor API key before remixing.")
    video_id = media.youtube_id(video_id)
    chosen_preset = remix_catalog.selection(preset_id, prompt)
    source, chosen = remix_source(video_id, composite_id)
    identity = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            identity.update(chunk)
    source_hash = identity.hexdigest()
    cache_key = hashlib.sha256(json.dumps([source_hash, composite_id, remix_catalog.MODEL,
                                          chosen_preset["prompt"], credential.fingerprint]).encode()).hexdigest()
    with LOCK:
        project = load_project(video_id)
        existing = next((r for r in project.get("remixes", []) if r.get("cache_key") == cache_key
                         and r.get("credential_owner") == credential.fingerprint
                         and (r.get("status") in {"queued", "running"}
                              or r.get("status") == "complete" and
                              (folder(video_id) / "exports" / r["id"] / "reactor-remix.mp4").is_file())), None)
        if existing:
            return {"job_id": existing["job_id"], "remix_id": existing["id"], "cached": True}
        remix_id, job_id = "remix-" + uuid.uuid4().hex[:12], uuid.uuid4().hex[:12]
        entry = {"id": remix_id, "job_id": job_id, "video_id": video_id,
                 "composite_id": composite_id, "source_run_id": chosen["run_id"],
                 "source_sha256": source_hash, "source_url": chosen["url"],
                 "cache_key": cache_key, "provider": "Reactor", "model": remix_catalog.MODEL,
                 "credential_source": "browser", "credential_owner": credential.fingerprint,
                 "preset_id": chosen_preset["id"], "label": chosen_preset["label"],
                 "prompt": chosen_preset["prompt"], "status": "queued", "stage": "queued",
                 "message": "Waiting for Reactor", "created_at": time.time()}
        project.setdefault("remixes", []).insert(0, entry)
        media.write_json(folder(video_id) / "exports" / remix_id / "remix.json", entry)
        media.write_json(manifest(video_id), project)
        result = task("Reactor: " + chosen_preset["label"], lambda: remix_video(video_id, remix_id, credential=credential),
                      job_id=job_id, video_id=video_id, executor=REMIX_POOL,
                      metadata={"operation": "remix", "remix_id": remix_id})
        return {**result, "remix_id": remix_id}


def remix_video(video_id, remix_id, *, credential=None):
    import reactor_remix
    snapshot = next(r for r in load_project(video_id).get("remixes", []) if r["id"] == remix_id)
    if (credential is None or snapshot.get("credential_source") != "browser"
            or snapshot.get("credential_owner") != credential.fingerprint):
        raise reactor_credentials.CredentialError("This saved remix belongs to a different Reactor key.")
    entry = save_remix(video_id, remix_id, status="running", stage="connecting", started_at=time.time())
    started = time.monotonic()

    def progress(value):
        details = {**value, "elapsed_seconds": round(time.monotonic() - started, 2)}
        details["message"] = {
            "authenticating": "Connecting your Reactor account", "connecting": "Starting Reactor",
            "preparing_stream": "Preparing the finished clip", "remixing": "Reactor is editing your clip",
            "saving_remix": "Saving the Reactor video", "restoring_audio": "Restoring the original audio",
            "complete": "Reactor remix ready",
        }.get(details.get("stage"), "Working with Reactor")
        save_remix(video_id, remix_id, **details)
        job_progress(entry["job_id"], details)

    try:
        source, _ = remix_source(video_id, entry["composite_id"])
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != entry["source_sha256"]:
            raise ValueError("The original clip changed. Choose it again before remixing.")
        output = folder(video_id) / "exports" / remix_id / "reactor-remix.mp4"
        try:
            receipt = reactor_remix.generate(source, output, entry["prompt"], on_progress=progress,
                                              credential_key=credential.key, credential_owner=credential.fingerprint)
        except reactor_remix.RemixError:
            raise
        except Exception:
            raise RuntimeError("Reactor could not finish this remix.") from None
        if not output.is_file():
            raise ValueError("Reactor did not produce a finished video.")
        # The adapter verifies decoded video and original audio before returning.
        result = save_remix(video_id, remix_id, status="complete", stage="complete", percent=100,
                            message="Reactor remix ready", finished_at=time.time(),
                            elapsed_seconds=round(time.monotonic() - started, 2),
                            url=f"/media/{video_id}/exports/{remix_id}/{output.name}",
                            media=receipt.get("media", {}),
                            audio_preserved=receipt.get("original_audio_preserved", receipt.get("audio_preserved", False)),
                            timeline_preservation_verified=receipt.get("timeline_preservation_verified", False))
        return result
    except Exception as error:
        # Keep provider internals and credentials out of persistent state and browsers.
        message = (str(error) if isinstance(error, (ValueError, reactor_remix.RemixError)) else
                   "Reactor could not finish this remix. The original clip is still available.")
        message = message.replace(credential.key, "[credential]")
        save_remix(video_id, remix_id, status="failed", stage="failed", error=message,
                   message=message, finished_at=time.time())
        raise ValueError(message) from None


def start_remix_batch(video_id, composite_ids, preset_id="day-to-night", prompt=None, *, credential=None):
    import remix_catalog
    if credential is None:
        raise reactor_credentials.CredentialError("Connect your Reactor API key before remixing.")
    if (not isinstance(composite_ids, list) or not 1 <= len(composite_ids) <= 40
            or any(not isinstance(item, str) for item in composite_ids)
            or len(set(composite_ids)) != len(composite_ids)):
        raise ValueError("Choose 1 to 40 different finished clips.")
    remix_catalog.selection(preset_id, prompt)
    for item in composite_ids:
        remix_source(video_id, item)
    return {"jobs": [start_remix(video_id, item, preset_id, prompt, credential=credential) for item in composite_ids]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, value, status=200, *, headers=()):
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def trusted(self):
        return demo_auth.trusted(self)

    def credential_origin(self):
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        return origin in {"http://" + host, "https://" + host} and self.trusted()

    def serve_file(self, path, head=False):
        if not path.is_file():
            self.send_error(404)
            return
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if not match or not any(match.groups()):
                self.send_error(416)
                return
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), end) if last else end
            else:
                start = max(0, size - int(last))
            if start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if path.suffix == ".zip":
            self.send_header("Content-Disposition", 'attachment; filename="football-cutups.zip"')
        self.end_headers()
        if head:
            return
        try:
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = stream.read(min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        if not self.trusted():
            self.send_error(403)
            return
        if demo_auth.intercept(self, head=head):
            return
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/state":
                with LOCK:
                    projects = [public_project(p.parent.name) for p in sorted((DATA / "sources").glob("*/project.json"))]
                    jobs = compact_jobs()
                self.send_json({"projects": projects, "jobs": jobs, "defaults": media.DEFAULTS,
                                "orbit_paths": orbit_paths.catalog(),
                                "generation": generation_status(fal_credentials.get(self.headers.get("Cookie"))), "seed": SEED,
                                "fal_credentials": fal_credentials.status(self.headers.get("Cookie")),
                                "reactor_credentials": reactor_credentials.status(self.headers.get("Cookie")),
                                "workspace_reset": media.read_json(DATA / "workspace-reset.json", {}).get("id"),
                                "action_labeling": {"ready": fal_credentials.get(self.headers.get("Cookie")) is not None}})
            elif path == "/api/credentials/fal":
                self.send_json(fal_credentials.status(self.headers.get("Cookie")))
            elif path == "/api/credentials/reactor":
                self.send_json(reactor_credentials.status(self.headers.get("Cookie")))
            elif path == "/api/generation/check":
                self.send_json(generation_status(fal_credentials.get(self.headers.get("Cookie"))))
            elif path == "/api/activity":
                self.send_json({"jobs": compact_jobs(), "server_time": time.time()})
            elif path == "/api/remix-presets":
                import remix_catalog
                self.send_json(remix_catalog.public_catalog(credential=reactor_credentials.get(self.headers.get("Cookie"))))
            elif path.startswith("/api/jobs/"):
                job = get_job(path.rsplit("/", 1)[1])
                self.send_json(job if job else {"error": "Unknown job"}, 200 if job else 404)
            elif path.startswith("/api/runs/"):
                parts = path.removeprefix("/api/runs/").split("/")
                if len(parts) != 2:
                    raise ValueError("Use a video ID and run ID.")
                self.send_json(run_status(parts[0], parts[1]))
            elif path.startswith("/media/"):
                parts = path.removeprefix("/media/").split("/")
                location = folder(parts[0]).resolve()
                target = location.joinpath(*parts[1:]).resolve()
                permitted = (len(parts) == 2 and target.name in {"source.mp4", "source.mkv", "source-preview.mp4", "source-preview-av1.mp4"}
                             or len(parts) > 2 and parts[1] in {"frames", "exports"})
                if not permitted or not target.is_relative_to(location) or target.suffix not in {".mp4", ".mkv", ".png", ".zip"}:
                    self.send_error(404)
                    return
                self.serve_file(target, head)
            elif path in {"/", "/app.js"}:
                self.serve_file(ROOT / "ui" / ("out/standalone.html" if path == "/" else "app.js"), head)
            elif path.startswith("/assets/"):
                assets = (ROOT / "ui" / "assets").resolve()
                target = (assets / path.removeprefix("/assets/")).resolve()
                if not target.is_relative_to(assets) or (target.suffix not in {".woff", ".woff2", ".png", ".svg", ".js"}
                        and target.name != "reactor-live-3.0.2.wasm"):
                    self.send_error(404)
                    return
                self.serve_file(target, head)
            else:
                self.send_error(404)
        except (ValueError, KeyError, IndexError) as error:
            self.send_json({"error": str(error)}, 400)

    def do_POST(self):
        if not self.trusted():
            self.send_error(403)
            return
        if demo_auth.intercept(self):
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 65536 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("Send a small JSON request.")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Send a JSON object.")
            path = urlparse(self.path).path
            if path in {"/api/credentials/fal", "/api/credentials/fal/clear",
                        "/api/credentials/reactor", "/api/credentials/reactor/clear"}:
                if not self.credential_origin():
                    self.send_json({"error": "Use this page's same-origin key form."}, 403)
                    return
                provider_credentials = reactor_credentials if "/reactor" in path else fal_credentials
                cookie = self.headers.get("Cookie")
                secure = self.headers.get("Origin", "").startswith("https://")
                if path.endswith("/clear"):
                    provider_credentials.clear(cookie)
                    header = provider_credentials.cookie_header(secure=secure, clear=True)
                    result = {"configured": False, "verified": False}
                else:
                    token, credential = provider_credentials.set_key(cookie, data.get("key"))
                    header = provider_credentials.cookie_header(token, secure=secure)
                    result = provider_credentials.status(f"{provider_credentials.COOKIE_NAME}={token}")
                    if provider_credentials is fal_credentials:
                        resume_browser_generations(credential)
                self.send_json(result, headers=[("Set-Cookie", header)])
                return
            if path == "/api/search":
                result = task("Search YouTube", lambda: search(data.get("query", "")), executor=SEARCH_POOL)
            elif path == "/api/import":
                video_id = media.youtube_id(data["video_id"])
                capabilities = playback_capability_options(data.get("playback_capabilities"))
                result = start_import(video_id, capabilities)
            elif path == "/api/analyze":
                video_id = media.youtube_id(data["video_id"])
                result = task("Analyze cut-ups", lambda: analyze(video_id))
            elif path == "/api/select":
                result = selection(data["video_id"], data["item_id"], data)
            elif path == "/api/frame":
                result = task("Extract freeze frame", lambda: frame(data["video_id"], data["item_id"],
                              float(data["time"]), float(data.get("tail", media.DEFAULTS["tail_seconds"]))),
                              video_id=data["video_id"], executor=INTERACTIVE_POOL)
            elif path == "/api/label":
                result = start_label(data["video_id"], data["item_id"], data["time"],
                                     credential=fal_credentials.require(self.headers.get("Cookie")))
            elif path == "/api/export":
                result = task("Export source cut-ups", lambda: export_sources(data["video_id"], bool(data.get("combined"))))
            elif path == "/api/generation/check":
                result = generation_status(fal_credentials.get(self.headers.get("Cookie")))
            elif path == "/api/generate":
                result = start_generation(data["video_id"], data.get("mode"), data.get("reference_run_id"),
                                          item_id=data.get("item_id"), credential=fal_credentials.require(self.headers.get("Cookie")),
                                          **({"orbit_path": data["orbit_path"]} if "orbit_path" in data else {}))
            elif path == "/api/assemble":
                result = start_assembly(data["video_id"], data["run_id"],
                                        data.get("tail_seconds", media.DEFAULTS["tail_seconds"]))
            elif path == "/api/remix":
                options = {"preset_id": data.get("preset_id", "day-to-night"), "prompt": data.get("prompt"),
                           "credential": reactor_credentials.require(self.headers.get("Cookie"))}
                if "composite_ids" in data:
                    result = start_remix_batch(data["video_id"], data["composite_ids"], **options)
                else:
                    result = start_remix(data["video_id"], data["composite_id"], **options)
            elif path == "/api/combine":
                result = task("Combine highlights", lambda: combine_highlights(data["video_id"], data.get("outputs")))
            elif path == "/api/reactor/live-token":
                import reactor_live
                credential = reactor_credentials.require(self.headers.get("Cookie"))
                result = reactor_live.token(credential_key=credential.key)
            else:
                self.send_error(404)
                return
            self.send_json(result)
        except LabelBusy as error:
            self.send_json({"error": str(error)}, 409)
        except (ValueError, KeyError, TypeError) as error:
            self.send_json({"error": str(error)}, 400)


def acquire_server_lock():
    import fcntl
    DATA.mkdir(parents=True, exist_ok=True)
    handle = (DATA / "server.lock").open("a")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("Another 360° World Model Clips server is running or finishing its active jobs.") from None
    return handle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8476")))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--prepare-seed", action="store_true")
    args = parser.parse_args()
    demo_auth.validate_deployment(args.host)
    if args.prepare_seed:
        verify_source(SEED)
        project = analyze(SEED)
        print(json.dumps({"source": project["title"], "media": project["media"],
                          "candidates": len(project["candidates"]), "analysis": project["analysis"]}))
        return
    from build_ui import build_ui
    build_ui()
    ownership = acquire_server_lock()
    http_server = None

    def stop(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        http_server = ThreadingHTTPServer((args.host, args.port), Handler)
        reconcile_jobs()
        for path in (DATA / "sources").glob("*/project.json"):
            project = load_project(path.parent.name)
            if (project.get("status") == "ready" and project.get("analysis", {}).get("mode") != "manual"
                    and not football_title(project.get("title"))):
                manual_project(project["id"])
        print(f"360° World Model Clips: {os.environ.get('PUBLIC_ORIGIN') or f'http://{args.host}:{args.port}'}", flush=True)
        http_server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if http_server:
            http_server.server_close()
        POOL.shutdown(wait=True)
        GENERATION_POOL.shutdown(wait=True)
        INTERACTIVE_POOL.shutdown(wait=True)
        SEARCH_POOL.shutdown(wait=True)
        LABEL_POOL.shutdown(wait=True)
        REMIX_POOL.shutdown(wait=True)
        ownership.close()


if __name__ == "__main__":
    main()
