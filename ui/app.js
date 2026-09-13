"use strict";
const $ = (id) => document.getElementById(id);
const player = $("player");
const GENERATION_STORAGE_KEY = "football-edits.pending-generations.v1";
const PROJECT_STORAGE_KEY = "football-edits.selected-project.v1";
const UPDATE_VIEW_STORAGE_KEY = "football-edits.update-view.v1";
const ORBIT_PATH_STORAGE_KEY = "world-model-clips.orbit-paths.v1";
const orbitPathSelections = new Map();
const WORKSPACE_RESET_STORAGE_KEY = "football-edits.workspace-reset";
const APP_BUILD = document.querySelector?.('meta[name="football-edits-build"]')?.content || null;
let state = { projects: [] }, projectId = preferredProject(), activeId = null, previewEnd = null;
const pendingGenerations = readPendingGenerations(), generationMonitors = new Map();
let workspaceRefreshTimer = null;
let initialOutputScrollDone = false;
let availableAppBuild = null, lastAppBuildCheck = null, appBuildCheckRunning = false;
const selectedOutputs = new Set(), knownOutputs = new Set();
const viewedOutputs = new Map();
const remixDrafts = new Map(), viewedRemixes = new Map(), knownRemixes = new Set(), pendingRemixSources = new Set();
let remixCatalog = null, remixCatalogLoading = false, remixCatalogError = "";
const liveClipSelections = new Map();
let liveLook = "night", liveSession = null, liveSessionSequence = 0;
let livePreviewState = { state: "idle", message: "Live preview is stopped." };
let livePromptMessage = "Night selected. Start to connect.";
const actionLabelQueues = new Map();
const activityCards = new Map(), activitySeen = new Map();
let activityTimer = null, activityAvailable = false, activitySignature = null, activityCollapsed = true;
let activityClockOffset = 0;
let activeLocalJobs = 0;
let workspaceResetEpoch = 0;
let falKeyBusy = false, falKeyEditing = false, falKeyNotice = "", falKeyError = false, falCredentialRevision = 0;
let reactorKeyBusy = false, reactorKeyEditing = false, reactorKeyNotice = "", reactorKeyError = false, reactorCredentialRevision = 0;
let loginRedirecting = false;
const project = () => state.projects.find((p) => p.id === projectId);
const active = () => project()?.candidates?.find((c) => c.id === activeId);
function labelMatchesTime(label, time, fps) {
  return label && Number.isFinite(label.source_time) && Number.isFinite(time)
    && Math.abs(label.source_time - time) <= 1 / (Number.isFinite(fps) && fps > 0 ? fps : 30);
}
function actionTitle(label, time, fps) {
  return labelMatchesTime(label, time, fps) && label.status === "complete"
    && ["medium", "high"].includes(label.confidence) && typeof label.label === "string"
    && label.label.trim() && label.label !== "Action not identified" ? displayText(label.label) : null;
}
function momentTitle(item, source = project()) {
  return actionTitle(item.action_label, item.freeze_time ?? item.time, source?.media?.fps) || item.label;
}
function renderActionLabelStatus() {
  const item = active(), source = project(); if (!item || !source) return;
  const time = item.freeze_time ?? item.time, label = item.action_label;
  const title = actionTitle(label, time, source.media.fps);
  const queue = actionLabelQueues.get(source.id);
  const waiting = [queue?.active, queue?.next].some((record) => record?.item_id === item.id
    && Math.abs(record.time - time) <= 1 / source.media.fps);
  $("clip-title").textContent = displayText(title || item.label);
  const note = title ? item.label : waiting ? "Identifying action…"
    : labelMatchesTime(label, time, source.media.fps) && ["timeout", "uncertain"].includes(label.status) ? "Action not identified" : "";
  $("clip-detection").textContent = displayText(note);
  $("clip-detection").hidden = !note;
}
function cachedActionLabel(source, record) {
  const item = source?.candidates?.find((candidate) => candidate.id === record.item_id);
  return labelMatchesTime(item?.action_label, record.time, source?.media?.fps)
    && ["complete", "uncertain", "timeout"].includes(item.action_label.status);
}
function scheduleActionLabel(source = project(), item = active(), time = item?.freeze_time ?? item?.time) {
  if (!state.action_labeling?.ready || !source || !item || !Number.isFinite(time) || source.status === "preparing_preview") return;
  for (const [id, other] of actionLabelQueues) {
    if (id !== source.id) { clearTimeout(other.timer); other.next = null; }
  }
  if (!actionLabelQueues.has(source.id)) actionLabelQueues.set(source.id, { active: null, next: null, timer: null, attempted: new Set() });
  const queue = actionLabelQueues.get(source.id);
  clearTimeout(queue.timer); queue.timer = null; queue.next = null;
  const fps = source.media.fps || 30;
  const record = { video_id: source.id, item_id: item.id, time, key: `${item.id}:${Math.round(time * fps)}`, fps };
  if (cachedActionLabel(source, record) || queue.attempted.has(record.key) || queue.active?.key === record.key) { renderActionLabelStatus(); return; }
  queue.next = record;
  queue.timer = setTimeout(() => { queue.timer = null; void runNextActionLabel(source.id); }, 400);
  renderActionLabelStatus();
}
async function requestActionLabel(record) {
  const started = await api("/api/label", { video_id: record.video_id, item_id: record.item_id, time: record.time }, 4000);
  if (!started.job_id) throw new Error("Action not identified");
  const current = await pollGeneration({ job_id: started.job_id }, { timeout: 16000, onStatus: () => {},
    request: (path) => api(path, undefined, 4000),
    wait: (milliseconds) => new Promise((resolve) => setTimeout(resolve, Math.min(milliseconds, 750))) });
  if (current.status !== "complete") throw new Error("Action not identified");
  return current.result;
}
async function runNextActionLabel(sourceId) {
  const queue = actionLabelQueues.get(sourceId);
  if (!queue || queue.active || !queue.next) return;
  const record = queue.next; queue.next = null;
  const resetEpoch = workspaceResetEpoch;
  const source = state.projects.find((item) => item.id === sourceId);
  if (!source || cachedActionLabel(source, record) || queue.attempted.has(record.key)) return;
  queue.active = record; queue.attempted.add(record.key); renderActionLabelStatus();
  try {
    const result = await requestActionLabel(record);
    if (resetEpoch !== workspaceResetEpoch) return;
    const label = result?.labelresult || result?.action_label || result?.label_result;
    const currentSource = state.projects.find((item) => item.id === sourceId);
    const item = currentSource?.candidates?.find((candidate) => candidate.id === record.item_id);
    if (item && result.video_id === sourceId && result.item_id === record.item_id
        && labelMatchesTime(label, record.time, record.fps)
        && Math.abs((item.freeze_time ?? item.time) - record.time) <= 1 / record.fps) {
      item.action_label = label;
      if (projectId === sourceId) render();
    }
    try { await refresh({ onlyChanged: true }); } catch { /* Ordinary read-only refresh will load the saved label later. */ }
  } catch (error) {
    if (resetEpoch !== workspaceResetEpoch) return;
    const currentSource = state.projects.find((source) => source.id === sourceId);
    const item = currentSource?.candidates?.find((candidate) => candidate.id === record.item_id);
    if (error.status === 409 && Date.now() < (record.busyUntil || Date.now() + 10000)) {
      queue.attempted.delete(record.key);
      if (!queue.next && projectId === sourceId && activeId === record.item_id
          && Math.abs((item?.freeze_time ?? item?.time) - record.time) <= 1 / record.fps) {
        queue.next = { ...record, busyUntil: record.busyUntil || Date.now() + 10000 };
      }
    } else if (item && Math.abs((item.freeze_time ?? item.time) - record.time) <= 1 / record.fps && !cachedActionLabel(currentSource, record)) {
      item.action_label = { status: "uncertain", source_time: record.time, label: "Action not identified", confidence: "low" };
    }
  } finally {
    queue.active = null; renderActionLabelStatus();
    if (queue.next) { clearTimeout(queue.timer); queue.timer = setTimeout(() => { queue.timer = null; void runNextActionLabel(sourceId); }, queue.next.busyUntil ? 1000 : 400); }
  }
}
function clock(value = 0) {
  return `${String(Math.floor(value / 60)).padStart(2, "0")}:${(value % 60).toFixed(3).padStart(6, "0")}`;
}
function displayText(value) {
  return String(value ?? "")
    .replace(/https?:\/\/[^\s]*(?:fal\.ai|h3-max|camera-controls)[^\s]*/gi, "the generation service")
    .replace(/\b(?:FAL_KEY|REACTOR_API_KEY)\b/gi, "the connection")
    .replace(/\bGenerate\s+(?:(?:Fal|foul)\s+)?(?:Fast[ -]?)?H3(?:\s+Max)?(?:\s+camera)?(?:\s+orbit)?\b/gi, "Generate with world models")
    .replace(/\b(?:(?:Fal|foul)\s+)?(?:Fast[ -]?)?H3(?:[ -]+Max)?(?:[ /-]+camera[ -]+controls)?\b/gi, "world models")
    .replace(/\b(?:Fal|foul|Reactor)(?:'s)?\b/gi, "world models")
    .replace(/\bcamera(?:[ -]+(?:orbit|controls|movement|requests?))?\b/gi, "world models")
    .replace(/\borbit tests?\b/gi, "generated clips").replace(/\btests?\b/gi, (word) => word.toLowerCase() === "tests" ? "clips" : "Clip")
    .replace(/\borbit\b/gi, "generated moment");
}
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = displayText(text);
  if (className) element.className = className;
  return element;
}
function status(message) { $("status").textContent = displayText(message); }
function redirectToLogin() {
  if (loginRedirecting || typeof window.location?.replace !== "function") return;
  const current = new URL(window.location.href);
  let next = current.pathname + current.search + current.hash;
  if (!/^\/(?!\/)/.test(next) || /[\\\x00-\x1f\x7f]/.test(next)) next = "/";
  savePendingGenerations(); loginRedirecting = true;
  window.location.replace(`/login?next=${encodeURIComponent(next)}`);
}
async function api(path, data, timeout = 0) {
  const controller = timeout ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), timeout) : null;
  try {
    const response = await fetch(path, {
      ...(controller ? { signal: controller.signal } : {}),
      ...(data === undefined ? {} : {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
      }),
    });
    let value;
    try { value = await response.json(); } catch (error) {
      error.status = response.status; throw error;
    }
    if (response.status === 401) redirectToLogin();
    if (!response.ok) { const error = new Error(value.error || "Request failed."); error.status = response.status; throw error; }
    return value;
  } finally { if (timer !== null) clearTimeout(timer); }
}
function readStored(key) {
  try { return JSON.parse(localStorage.getItem(key)); } catch { return null; }
}
function writeStored(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Keep tracking in this tab when storage is unavailable. */ }
}
function preferredProject() {
  const query = window.location?.search ? new URLSearchParams(window.location.search).get("video") : null;
  return query && /^[A-Za-z0-9_-]{1,128}$/.test(query) ? query : readStored(PROJECT_STORAGE_KEY);
}
function rememberProject() {
  writeStored(PROJECT_STORAGE_KEY, projectId);
  if (!projectId || !window.location || !window.history) return;
  const url = new URL(window.location.href);
  if (url.searchParams.get("video") !== projectId) {
    url.searchParams.set("video", projectId); window.history.replaceState(null, "", url);
  }
}
function readPendingGenerations() {
  const records = readStored(GENERATION_STORAGE_KEY);
  const validId = (value) => typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value);
  return new Map((Array.isArray(records) ? records : []).filter((record) => record
    && validId(record.video_id) && (!record.job_id || validId(record.job_id))
    && (!record.run_id || validId(record.run_id))
    && (record.job_id || record.run_id || (Array.isArray(record.known_run_ids) && Number.isFinite(record.freeze_time))))
    .map((record) => [record.video_id, record]));
}
function savePendingGenerations() {
  writeStored(GENERATION_STORAGE_KEY, [...pendingGenerations.values()]);
}
async function readGeneration(record, request = (path) => api(path, undefined, 20000)) {
  if (!record.run_id && !record.job_id) {
    const snapshot = await request("/api/state");
    const source = snapshot.projects.find((item) => item.id === record.video_id);
    const matches = (source?.generations || []).filter((run) => run.provider === "Fal"
      && !record.known_run_ids.includes(run.id) && run.freeze_time === record.freeze_time
      && (!record.orbit_path || (run.orbit_path || "around") === record.orbit_path));
    if (matches.length !== 1) return { status: "awaiting_acknowledgement" };
    record.run_id = matches[0].id; savePendingGenerations();
  }
  if (record.job_id && !record.use_run_status) {
    try { return await request(`/api/jobs/${encodeURIComponent(record.job_id)}`); }
    catch (error) {
      if (error.status !== 404 || !record.run_id) throw error;
      record.use_run_status = true; savePendingGenerations();
    }
  }
  return request(`/api/runs/${encodeURIComponent(record.video_id)}/${encodeURIComponent(record.run_id)}`);
}
async function pollGeneration(record, options = {}) {
  const request = options.request || ((path) => api(path, undefined, 20000));
  const wait = options.wait || ((milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)));
  const now = options.now || Date.now, onStatus = options.onStatus || status;
  const deadline = now() + (options.timeout ?? 20 * 60 * 1000);
  let previousMessage = null;
  let readCount = 0, consecutiveReadErrors = 0;
  const announce = (message) => { if (message !== previousMessage) { previousMessage = message; onStatus(message); } };
  while (now() < deadline) {
    if (options.isCurrent && !options.isCurrent()) return { status: "cancelled" };
    try {
      const current = await readGeneration(record, request);
      consecutiveReadErrors = 0;
      if (options.isCurrent && !options.isCurrent()) return { status: "cancelled" };
      if (["complete", "failed", "interrupted"].includes(current.status)) return current;
      announce(current.status === "awaiting_acknowledgement"
        ? "Recovering the existing submission from saved run history. No new generation has been sent."
        : `Your world model clip is ${current.status === "queued" ? "queued" : "processing"}. You can reload this page and tracking will resume.`);
    } catch (error) {
      consecutiveReadErrors++;
      if (options.isCurrent && !options.isCurrent()) return { status: "cancelled" };
      if (error.status && error.status !== 404 && error.status < 500) {
        return { status: "unresolved", error: "The existing run could not be checked. Its tracking details are saved; refresh the connection to resume." };
      }
      announce("Reconnecting to the server. Your existing run is saved and will be checked again; no new generation has been sent.");
    }
    readCount++;
    const interval = options.pollInterval ? options.pollInterval({ readCount, consecutiveReadErrors }) : 3000;
    await wait(Math.min(interval, Math.max(0, deadline - now())));
  }
  return { status: "unresolved", error: "The existing run is still saved. Reload this page or refresh the connection to resume checking it; no new generation has been sent." };
}
function monitorGeneration(record) {
  if (generationMonitors.has(record.video_id)) return generationMonitors.get(record.video_id);
  const monitor = (async () => {
    try {
      const current = await pollGeneration(record, { isCurrent: () => pendingGenerations.get(record.video_id) === record });
      if (current.status === "cancelled") return;
      if (!["complete", "failed", "interrupted"].includes(current.status)) { status(current.error); return; }
      try { await refresh(); } catch {
        status("Your run status is saved. Reconnecting to load its output; refresh the connection or reload to resume."); return;
      }
      if (pendingGenerations.get(record.video_id) === record) {
        pendingGenerations.delete(record.video_id); savePendingGenerations();
      }
      if (current.status === "complete") {
        status(record.video_id === projectId
          ? "Your world model clip is ready. Review the assembled video under Outputs."
          : "Your world model clip is ready. Select its imported video to review Outputs.");
      } else status(current.error || "The saved run stopped before its highlight was ready. Review its history; it was not resubmitted.");
    } finally { if (generationMonitors.get(record.video_id) === monitor) generationMonitors.delete(record.video_id); renderSelection(); }
  })();
  generationMonitors.set(record.video_id, monitor);
  return monitor;
}
function resumeGenerations() {
  for (const record of pendingGenerations.values()) safely(() => monitorGeneration(record));
}
async function job(path, data, message) {
  activeLocalJobs++;
  try {
  status(message);
  const started = await api(path, data);
  const current = await pollGeneration({ job_id: started.job_id }, {
    pollInterval: localJobPollInterval,
    onStatus: (update) => status(update.startsWith("Reconnecting")
      ? "Reconnecting to check your existing task. It has not been restarted." : message),
  });
  if (current.status === "complete") return current.result;
  throw new Error(current.status === "unresolved"
    ? `Could not finish checking job ${started.job_id}. Its saved state can be reviewed; the task was not restarted.`
    : current.error || "The saved task was interrupted before completion. It was not restarted.");
  } finally { activeLocalJobs--; }
}
function localJobPollInterval({ readCount, consecutiveReadErrors }) {
  if (consecutiveReadErrors) return Math.min(3000, 1000 * consecutiveReadErrors);
  return [250, 250, 500, 500, 1000, 1500, 2000][readCount - 1] ?? 3000;
}
async function safely(operation) { try { await operation(); } catch (error) { status(error.message); } }
function renderFalCredentials() {
  const configured = state.fal_credentials?.configured === true;
  const editing = !configured || falKeyEditing;
  $("fal-account").dataset.connected = String(configured);
  $("fal-account").setAttribute("aria-busy", String(falKeyBusy));
  $("fal-key-form").hidden = !editing;
  $("fal-key-connected").hidden = editing;
  $("fal-key-cancel").hidden = !configured;
  $("fal-key-badge").textContent = configured ? "Connected" : "Not connected";
  for (const id of ["fal-key", "fal-key-connect", "fal-key-reveal", "fal-key-change", "fal-key-disconnect", "fal-key-cancel"]) $(id).disabled = falKeyBusy;
  $("fal-key-connect").textContent = falKeyBusy ? "Connecting…" : "Connect Fal";
  $("fal-key-status").dataset.error = String(falKeyError);
  $("fal-key-status").textContent = falKeyNotice || (configured
    ? state.fal_credentials.verified === false
      ? "Key connected. Fal will check generation access on your first request."
      : "Your key is connected. Disconnecting stops future requests; work already submitted will finish."
    : "Add your key when you're ready to generate. You can browse videos first.");
}
function clearFalKeyInput() {
  $("fal-key").value = "";
  $("fal-key").type = "password";
  $("fal-key-reveal").textContent = "Show";
  $("fal-key-reveal").setAttribute("aria-label", "Show API key");
  $("fal-key-reveal").setAttribute("aria-pressed", "false");
}
async function connectFalKey() {
  if (falKeyBusy) return;
  let key = $("fal-key").value.trim();
  if (!key) { $("fal-key").focus(); return; }
  falKeyBusy = true; falKeyError = false; falKeyNotice = "Connecting your Fal account…";
  falCredentialRevision++;
  clearFalKeyInput(); renderFalCredentials();
  try {
    const response = api("/api/credentials/fal", { key }, 20000);
    key = "";
    const connected = await response;
    state.fal_credentials = connected;
    falKeyEditing = false;
    falKeyNotice = connected.verified === false
      ? "Key connected. Fal will check generation access on your first request."
      : "Your key is connected. Generation will use your Fal credits.";
    try { await refresh(); } catch { falKeyNotice = "Your key is connected. Reconnecting to load generation controls…"; }
  } catch (error) {
    falKeyError = true;
    falKeyNotice = error.status === 400 || error.status === 422
      ? "Fal could not accept that key. Check it and try again."
      : "Could not confirm the connection. Refresh the page to check its status before trying again.";
  } finally {
    key = ""; falKeyBusy = false; falCredentialRevision++; renderFalCredentials();
  }
}
async function disconnectFalKey() {
  if (falKeyBusy) return;
  falKeyBusy = true; falKeyError = false; falKeyNotice = "Disconnecting your key…";
  falCredentialRevision++; clearFalKeyInput(); renderFalCredentials();
  try {
    state.fal_credentials = await api("/api/credentials/fal/clear", {}, 10000);
    state.generation = { ...state.generation, ready: false, configured: false };
    state.action_labeling = { ...state.action_labeling, ready: false };
    falKeyEditing = false;
    falKeyNotice = "Your key was removed. Work already submitted will finish.";
    renderSelection(); renderOutputViewer();
  } catch {
    falKeyError = true; falKeyNotice = "Could not confirm disconnection. Try again when the server reconnects.";
  } finally {
    falKeyBusy = false; falCredentialRevision++; renderFalCredentials();
  }
}
$("fal-key-form").addEventListener("submit", (event) => { event.preventDefault(); void connectFalKey(); });
$("fal-key-disconnect").addEventListener("click", () => { void disconnectFalKey(); });
$("fal-key-change").addEventListener("click", () => {
  falKeyEditing = true; falKeyNotice = ""; falKeyError = false; clearFalKeyInput(); renderFalCredentials(); $("fal-key").focus();
});
$("fal-key-cancel").addEventListener("click", () => {
  falKeyEditing = false; falKeyNotice = ""; falKeyError = false; clearFalKeyInput(); renderFalCredentials();
});
$("fal-key-reveal").addEventListener("click", () => {
  const show = $("fal-key").type === "password";
  $("fal-key").type = show ? "text" : "password";
  $("fal-key-reveal").textContent = show ? "Hide" : "Show";
  $("fal-key-reveal").setAttribute("aria-label", show ? "Hide API key" : "Show API key");
  $("fal-key-reveal").setAttribute("aria-pressed", String(show));
});

function renderReactorCredentials() {
  const configured = state.reactor_credentials?.configured === true;
  const editing = !configured || reactorKeyEditing;
  $("reactor-account").dataset.connected = String(configured);
  $("reactor-account").setAttribute("aria-busy", String(reactorKeyBusy));
  $("reactor-key-form").hidden = !editing;
  $("reactor-key-connected").hidden = editing;
  $("reactor-key-cancel").hidden = !configured;
  $("reactor-key-badge").textContent = configured ? "Connected" : "Not connected";
  for (const id of ["reactor-key", "reactor-key-connect", "reactor-key-reveal", "reactor-key-change", "reactor-key-disconnect", "reactor-key-cancel"]) $(id).disabled = reactorKeyBusy;
  $("reactor-key-connect").textContent = reactorKeyBusy ? "Connecting…" : "Connect Reactor";
  $("reactor-key-status").dataset.error = String(reactorKeyError);
  $("reactor-key-status").textContent = reactorKeyNotice || (configured
    ? "Your key is connected. Live previews and remixes use your Reactor credits."
    : "Optional. Add your key to use the Reactor step after your highlight is ready.");
}
function clearReactorKeyInput() {
  $("reactor-key").value = "";
  $("reactor-key").type = "password";
  $("reactor-key-reveal").textContent = "Show";
  $("reactor-key-reveal").setAttribute("aria-label", "Show Reactor API key");
  $("reactor-key-reveal").setAttribute("aria-pressed", "false");
}
function reactorReady() {
  return !reactorKeyBusy && state.reactor_credentials?.configured === true && !!remixCatalog;
}
async function connectReactorKey() {
  if (reactorKeyBusy) return;
  let key = $("reactor-key").value.trim();
  if (!key) { $("reactor-key").focus(); return; }
  reactorKeyBusy = true; reactorKeyError = false; reactorKeyNotice = "Connecting your Reactor account…";
  reactorCredentialRevision++;
  clearReactorKeyInput(); renderReactorCredentials(); renderRemixes();
  stopLivePreview("Live preview stopped while changing your Reactor key.");
  try {
    const response = api("/api/credentials/reactor", { key }, 20000);
    key = "";
    state.reactor_credentials = await response;
    reactorCredentialRevision++;
    reactorKeyEditing = false;
    reactorKeyNotice = "Your Reactor key is connected. No generation has started.";
    try { await Promise.all([refresh(), loadRemixPresets()]); }
    catch { reactorKeyNotice = "Your key is connected. Reconnecting to load the Reactor controls…"; }
  } catch (error) {
    reactorKeyError = true;
    reactorKeyNotice = error.status === 400 || error.status === 422
      ? "Reactor could not accept that key. Check it and try again."
      : "Could not confirm the Reactor connection. Refresh to check its status before trying again.";
  } finally {
    key = ""; reactorKeyBusy = false; reactorCredentialRevision++; renderReactorCredentials(); renderRemixes();
  }
}
async function disconnectReactorKey() {
  if (reactorKeyBusy) return;
  reactorKeyBusy = true; reactorKeyError = false; reactorKeyNotice = "Disconnecting your Reactor key…";
  reactorCredentialRevision++; clearReactorKeyInput(); renderReactorCredentials(); renderRemixes();
  stopLivePreview("Live preview stopped because you disconnected Reactor.");
  try {
    state.reactor_credentials = await api("/api/credentials/reactor/clear", {}, 10000);
    reactorCredentialRevision++;
    reactorKeyEditing = false;
    reactorKeyNotice = "Your Reactor key was removed. Saved remixes already submitted will finish.";
  } catch {
    reactorKeyError = true; reactorKeyNotice = "Could not confirm disconnection. Try again when the server reconnects.";
  } finally {
    reactorKeyBusy = false; reactorCredentialRevision++; renderReactorCredentials(); renderRemixes();
  }
}
$("reactor-key-form").addEventListener("submit", (event) => { event.preventDefault(); void connectReactorKey(); });
$("reactor-key-disconnect").addEventListener("click", () => { void disconnectReactorKey(); });
$("reactor-key-change").addEventListener("click", () => {
  reactorKeyEditing = true; reactorKeyNotice = ""; reactorKeyError = false; clearReactorKeyInput(); renderReactorCredentials(); $("reactor-key").focus();
});
$("reactor-key-cancel").addEventListener("click", () => {
  reactorKeyEditing = false; reactorKeyNotice = ""; reactorKeyError = false; clearReactorKeyInput(); renderReactorCredentials();
});
$("reactor-key-reveal").addEventListener("click", () => {
  const show = $("reactor-key").type === "password";
  $("reactor-key").type = show ? "text" : "password";
  $("reactor-key-reveal").textContent = show ? "Hide" : "Show";
  $("reactor-key-reveal").setAttribute("aria-label", show ? "Hide Reactor API key" : "Show Reactor API key");
  $("reactor-key-reveal").setAttribute("aria-pressed", String(show));
});

const ORBIT_ICONS = {
  around: '<ellipse cx="72" cy="42" rx="55" ry="18"/>',
  'over-under': '<ellipse cx="72" cy="42" rx="19" ry="35"/>',
  diagonal: '<ellipse cx="72" cy="42" rx="49" ry="24" transform="rotate(-38 72 42)"/>',
};
function orbitOptions() { return Array.isArray(state.orbit_paths?.options) ? state.orbit_paths.options : []; }
function chosenOrbitPath() {
  const options = orbitOptions(), key = projectId || "workspace";
  const saved = readStored(ORBIT_PATH_STORAGE_KEY);
  const id = orbitPathSelections.get(key) || (saved && typeof saved === "object" && !Array.isArray(saved) ? saved[key] : null);
  return options.find((option) => option.id === id) || options.find((option) => option.id === state.orbit_paths?.default) || options[0] || null;
}
function chooseOrbitPath(id) {
  if (!orbitOptions().some((option) => option.id === id)) return;
  const key = projectId || "workspace";
  orbitPathSelections.set(key, id);
  const saved = readStored(ORBIT_PATH_STORAGE_KEY);
  writeStored(ORBIT_PATH_STORAGE_KEY, { ...(saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {}), [key]: id });
  renderOrbitChoices();
}
function renderOrbitChoices() {
  const options = orbitOptions(), chosen = chosenOrbitPath(), container = $("orbit-choices");
  const signature = JSON.stringify(options);
  if (container.dataset.catalog !== signature) {
    container.replaceChildren(); container.dataset.catalog = signature;
    for (const option of options) {
      const button = node("button", undefined, "orbit-choice secondary");
      button.type = "button"; button.dataset.path = option.id;
      button.setAttribute("aria-describedby", "orbit-choice-note");
      const icon = node("span", undefined, "orbit-choice-icon");
      icon.innerHTML = `<svg viewBox="0 0 144 84" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><g class="orbit-guide">${ORBIT_ICONS[option.id] || ORBIT_ICONS.around}</g><path class="orbit-person" d="M72 34v22m-9-15 9-7 9 7M72 56l-8 12m8-12 8 12"/><circle class="orbit-person" cx="72" cy="26" r="5"/><circle class="orbit-origin" cx="${option.id === "over-under" ? 72 : option.id === "diagonal" ? 35 : 18}" cy="${option.id === "over-under" ? 77 : option.id === "diagonal" ? 68 : 42}" r="3" fill="currentColor" stroke="none"/></svg>`;
      const content = node("span", undefined, "orbit-choice-copy");
      const title = node("strong"); title.textContent = option.label;
      const description = node("span"); description.textContent = option.description;
      content.append(title, description);
      if (option.experimental) { const tag = node("span", undefined, "orbit-experimental"); tag.textContent = "Experimental"; content.append(tag); }
      const check = node("span", "✓", "orbit-choice-check"); check.setAttribute("aria-hidden", "true");
      button.append(icon, content, check);
      button.addEventListener("click", () => chooseOrbitPath(option.id));
      container.append(button);
    }
  }
  for (const button of container.children) button.setAttribute("aria-pressed", String(button.dataset.path === chosen?.id));
  $("orbit-choice-note").textContent = chosen
    ? [state.orbit_paths.note || "Moves are relative to your selected image.", chosen.note].filter(Boolean).join(" ")
    : "Loading available moves…";
}
function runOrbitLabel(run) {
  return run.orbit_path_label || orbitOptions().find((option) => option.id === (run.orbit_path || state.orbit_paths?.default))?.label || "";
}

function visibleWorkspace(snapshot) {
  return JSON.stringify({ projects: snapshot.projects, defaults: snapshot.defaults, generation: snapshot.generation, action_labeling: snapshot.action_labeling, orbit_paths: snapshot.orbit_paths });
}
function applyWorkspaceReset(snapshot) {
  const reset = typeof snapshot.workspace_reset === "string" ? snapshot.workspace_reset : snapshot.workspace_reset?.id;
  if (!reset || readStored(WORKSPACE_RESET_STORAGE_KEY) === reset) return false;
  workspaceResetEpoch++;
  for (const key of [GENERATION_STORAGE_KEY, PROJECT_STORAGE_KEY, ORBIT_PATH_STORAGE_KEY]) {
    try { localStorage.removeItem(key); } catch { writeStored(key, null); }
  }
  try { sessionStorage.removeItem(UPDATE_VIEW_STORAGE_KEY); } catch { /* No pending view is retained in this tab. */ }
  orbitPathSelections.clear();
  pendingGenerations.clear(); generationMonitors.clear(); selectedOutputs.clear(); knownOutputs.clear(); viewedOutputs.clear();
  remixDrafts.clear(); viewedRemixes.clear(); knownRemixes.clear(); pendingRemixSources.clear();
  stopLivePreview("Live preview stopped for the workspace reset."); liveClipSelections.clear();
  for (const queue of actionLabelQueues.values()) clearTimeout(queue.timer);
  actionLabelQueues.clear(); activitySeen.clear(); activitySignature = null;
  projectId = null; activeId = null; previewEnd = null; initialOutputScrollDone = true;
  $("search").value = ""; $("search-results").replaceChildren(); $("search-results").hidden = true;
  if ($("search-preview").open) closeSearchPreview();
  if (window.location?.href && window.history?.replaceState) {
    const url = new URL(window.location.href); url.searchParams.delete("video"); url.hash = "";
    window.history.replaceState(null, "", url);
  }
  writeStored(WORKSPACE_RESET_STORAGE_KEY, reset);
  renderEmptyWorkspace();
  return true;
}
async function refresh({ onlyChanged = false } = {}) {
  const credentialRevision = falCredentialRevision, reactorRevision = reactorCredentialRevision;
  const snapshot = await api("/api/state", undefined, 20000);
  if (credentialRevision !== falCredentialRevision) {
    snapshot.fal_credentials = state.fal_credentials; snapshot.generation = state.generation; snapshot.action_labeling = state.action_labeling;
  }
  if (reactorRevision !== reactorCredentialRevision) snapshot.reactor_credentials = state.reactor_credentials;
  const reset = applyWorkspaceReset(snapshot);
  const unchanged = !reset && visibleWorkspace(state) === visibleWorkspace(snapshot);
  if (!falKeyBusy && state.fal_credentials?.configured !== snapshot.fal_credentials?.configured) { falKeyNotice = ""; falKeyError = false; }
  if (!reactorKeyBusy && state.reactor_credentials?.configured !== snapshot.reactor_credentials?.configured) {
    reactorKeyNotice = ""; reactorKeyError = false;
    if (!snapshot.reactor_credentials?.configured && liveSession) stopLivePreview("Your Reactor key connection has ended.");
  }
  state = snapshot;
  renderFalCredentials();
  renderReactorCredentials();
  renderOrbitChoices();
  renderActivity();
  if (onlyChanged && unchanged) { renderNewestOutput(); renderOutputRunStatus(); renderRemixes(); return; }
  if (!project()) projectId = state.projects.find((p) => p.id === state.seed)?.id || state.projects[0]?.id;
  rememberProject();
  const select = $("source-select"); select.replaceChildren();
  for (const p of state.projects) {
    const label = p.status === "preparing_preview" ? `${p.title} · ${sourcePresentation(p).preparationLabel}` : p.title;
    const option = node("option", label); option.value = p.id; select.append(option);
  }
  select.value = projectId || "";
  const d = state.defaults;
  $("format-summary").textContent = `World models${d.resolution ? ` · ${d.resolution} generation` : ""}`;
  $("generation-message").textContent = displayText(state.generation.message);
  render();
  alignInitialOutputs();
}
async function alignInitialOutputs() {
  if (initialOutputScrollDone || window.location?.hash !== "#outputs" || !project()) return;
  initialOutputScrollDone = true;
  try { await document.fonts?.ready; } catch { /* The output still opens if a font cannot load. */ }
  window.requestAnimationFrame(() => $("outputs").scrollIntoView({ block: "start", behavior: "instant" }));
}
function workspaceRefreshDelay() {
  const running = (item) => ["queued", "running"].includes(item.status);
  const active = pendingGenerations.size || (state.jobs || []).some(running)
    || state.projects.some((source) => source.status === "preparing_preview" || (source.generations || []).some(running)
      || (source.remixes || []).some(running));
  return active && !activityAvailable ? 3000 : 10000;
}
function scheduleWorkspaceRefresh() {
  if (workspaceRefreshTimer !== null) clearTimeout(workspaceRefreshTimer);
  workspaceRefreshTimer = setTimeout(async () => {
    workspaceRefreshTimer = null;
    try { await refresh({ onlyChanged: true }); void checkAppBuild(); }
    catch { /* A temporary local restart is retried on the next read-only refresh. */ }
    finally { scheduleWorkspaceRefresh(); }
  }, workspaceRefreshDelay());
}
function activityIsRunning(job) { return ["queued", "running"].includes(job.status); }
function activityTime(value) {
  const result = typeof value === "number" ? (value < 1e12 ? value * 1000 : value) : Date.parse(value);
  return Number.isFinite(result) ? result : null;
}
function durationText(seconds) {
  const value = Math.max(0, Math.floor(seconds));
  return value < 60 ? `${value}s` : value < 3600 ? `${Math.floor(value / 60)}m ${String(value % 60).padStart(2, "0")}s` : `${Math.floor(value / 3600)}h ${Math.floor(value % 3600 / 60)}m`;
}
function byteText(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return null;
  const unit = bytes >= 1e9 ? [1e9, "GB"] : bytes >= 1e6 ? [1e6, "MB"] : bytes >= 1e3 ? [1e3, "KB"] : [1, "B"];
  return `${(bytes / unit[0]).toFixed(unit[0] === 1 ? 0 : 1)} ${unit[1]}`;
}
function activityPresentation(job, now = Date.now() + activityClockOffset) {
  const progress = job.progress || {}, running = activityIsRunning(job);
  const stage = progress.stage || job.stage || job.status;
  const names = { queued: "Waiting to start", running: "Working", resolving: "Finding the best source", fetching_metadata: "Finding the best source",
    downloading: progress.scope === "current_stream" ? `Downloading stream${progress.stream_id && progress.stream_id !== "unknown" ? ` ${progress.stream_id}` : ""}` : "Downloading video",
    merging: "Joining downloaded video and audio", verifying: "Checking the source file", probing: "Checking video quality",
    preparing_preview: "Preparing full-resolution playback", remuxing: "Preparing browser playback", transcoding: "Preparing browser playback",
    analyzing: "Finding suggested moments", extracting: "Extracting the source frame", labeling: "Identifying the action",
    generating: "Generating with world models", normalizing: "Preparing the video format",
    anchoring: "Preparing the generated clip", assembling: "Joining the highlight and audio",
    complete: "Ready", failed: "Needs attention", interrupted: "Interrupted" };
  const label = !running ? names[job.status] || job.status : names[stage] || stage.replaceAll("_", " ");
  const percent = running && Number.isFinite(progress.percent) && progress.percent >= 0 && progress.percent <= 100 ? progress.percent : null;
  const started = activityTime(job.created_at), ended = running ? now : activityTime(job.finished_at ?? job.updated_at);
  const elapsed = started !== null && ended !== null ? `${durationText((ended - started) / 1000)} elapsed` : "";
  const metrics = [];
  if (running && Number.isFinite(progress.downloaded_bytes)) {
    metrics.push(`${byteText(progress.downloaded_bytes)}${Number.isFinite(progress.total_bytes) && progress.total_bytes > 0 ? ` / ${byteText(progress.total_bytes)}${progress.total_bytes_estimated ? " estimated" : ""}` : " downloaded"}`);
  }
  if (running && Number.isFinite(progress.speed_bytes_per_second) && progress.speed_bytes_per_second > 0) metrics.push(`${byteText(progress.speed_bytes_per_second)}/s`);
  if (running && Number.isFinite(progress.eta_seconds) && progress.eta_seconds >= 0) metrics.push(`About ${durationText(progress.eta_seconds)} remaining${progress.scope === "current_stream" ? " in this stream" : ""}`);
  const stageStarted = activityTime(progress.stage_started_at);
  if (running && stageStarted !== null) metrics.push(`${durationText((now - stageStarted) / 1000)} in this stage`);
  if (job.operation === "remix" && Number.isFinite(progress.frames_received)) {
    metrics.push(`${progress.frames_received}${Number.isFinite(progress.frames_expected) ? ` / ${progress.frames_expected}` : ""} frames received`);
  }
  const text = job.operation === "remix" ? remixText : displayText;
  return { running, stage, label: job.operation === "remix" && running && stage === "generating" ? "Generating with Reactor" : text(label), percent, elapsed, metrics: metrics.join(" · "),
    title: text(job.kind || "Workspace task"), error: running || job.status === "complete" ? "" : text(job.error || "The task stopped. Its saved files are preserved.") };
}
function activityStateSignature(jobs) {
  return JSON.stringify(jobs.map((job) => [job.id, job.status, job.video_id, job.run_id, job.progress?.stage || job.stage, job.delivery_ready]));
}
function setActivityCollapsed(collapsed) {
  activityCollapsed = collapsed;
  $("activity-jobs").hidden = collapsed;
  $("activity-toggle").setAttribute("aria-expanded", String(!collapsed));
  $("activity-chevron").textContent = collapsed ? "⌃" : "⌄";
}
function renderActivity(jobs = state.jobs || [], now = Date.now() + activityClockOffset) {
  let announce = "";
  for (const job of jobs) {
    const previous = activitySeen.get(job.id);
    if ((activityIsRunning(job) && !previous) || (previous && activityIsRunning(previous) && !activityIsRunning(job))) {
      activityCollapsed = false;
      announce = `${job.operation === "remix" ? remixText(job.kind || "Reactor remix") : displayText(job.kind || "Task")}: ${activityPresentation(job, now).label}.`;
    }
    activitySeen.set(job.id, { status: job.status });
  }
  const running = jobs.filter(activityIsRunning);
  const recent = jobs.filter((job) => !activityIsRunning(job) && now - (activityTime(job.updated_at) || 0) < 5 * 60 * 1000)
    .sort((a, b) => (activityTime(b.updated_at) || 0) - (activityTime(a.updated_at) || 0)).slice(0, 3);
  const visible = [...running, ...recent];
  const container = $("activity-jobs");
  for (const [id, card] of activityCards) {
    if (!visible.some((job) => job.id === id)) { card.root.remove(); activityCards.delete(id); }
  }
  for (const job of visible) {
    let card = activityCards.get(job.id);
    if (!card) {
      const root = node("article", undefined, "activity-card"), heading = node("div", undefined, "activity-card-heading");
      const title = node("strong"), elapsed = node("span", undefined, "activity-elapsed");
      const source = node("p", undefined, "activity-source"), stage = node("div", undefined, "activity-stage");
      const label = node("span"), percent = node("span", undefined, "activity-percent");
      const meter = node("div", undefined, "activity-meter"), fill = node("span");
      const metrics = node("p", undefined, "activity-metrics"), error = node("p", undefined, "activity-error");
      const view = node("button", "View video →", "text-button activity-view"); view.type = "button";
      meter.setAttribute("role", "progressbar"); meter.setAttribute("aria-valuemin", "0"); meter.setAttribute("aria-valuemax", "100");
      heading.append(title, elapsed); stage.append(label, percent); meter.append(fill);
      root.append(heading, source, stage, meter, metrics, error, view); root.dataset.job = job.id;
      card = { root, title, elapsed, source, label, percent, meter, fill, metrics, error, view, job };
      view.addEventListener("click", () => {
        const source = state.projects.find((item) => item.id === card.job.video_id); if (!source) return;
        if (card.job.operation === "remix") {
          projectId = source.id; activeId = null; rememberProject(); render();
          if (source.remixes?.some((item) => item.id === card.job.remix_id && item.status === "complete" && item.url)) {
            viewedRemixes.set(projectId, card.job.remix_id); renderRemixViewer();
          }
          $("source-select").value = source.id;
          $("reactor-remix").scrollIntoView({ behavior: "smooth", block: "start" }); return;
        }
        const run = source.generations?.find((item) => item.id === card.job.run_id);
        const output = [...(run?.composites || [])].reverse().find((clip) => clip.url && clip.media?.duration);
        if (output) { openFinishedOutput({ source, clip: output }); return; }
        projectId = source.id; activeId = null; rememberProject(); render(); $("source-select").value = source.id;
        $(card.job.run_id ? "outputs" : "frame-heading").scrollIntoView({ behavior: "smooth", block: "start" });
      });
      activityCards.set(job.id, card); container.append(root);
    }
    card.job = job;
    card.view.textContent = job.operation === "remix" ? "View remix →" : "View video →";
    const value = activityPresentation(job, now), source = state.projects.find((item) => item.id === job.video_id);
    card.root.dataset.status = job.status; card.title.textContent = value.title; card.elapsed.textContent = value.elapsed;
    card.source.textContent = source?.title || job.video_id || ""; card.source.hidden = !card.source.textContent;
    card.label.textContent = value.label; card.percent.textContent = value.percent === null ? "" : `${Math.round(value.percent)}%`;
    card.meter.hidden = !value.running; card.meter.dataset.indeterminate = String(value.percent === null);
    card.meter.setAttribute("aria-label", value.label); card.meter.setAttribute("aria-valuetext", value.percent === null ? "Progress not measured" : `${Math.round(value.percent)} percent of this stage`);
    if (value.percent === null) card.meter.removeAttribute("aria-valuenow"); else card.meter.setAttribute("aria-valuenow", String(value.percent));
    card.fill.style.width = value.percent === null ? "35%" : `${value.percent}%`;
    card.metrics.textContent = value.metrics; card.metrics.hidden = !value.metrics;
    card.error.textContent = value.error; card.error.hidden = !value.error; card.view.hidden = !source;
  }
  $("activity-count").textContent = running.length ? `${running.length} active` : "Ready";
  $("activity-panel").dataset.active = String(!!running.length);
  $("activity-jobs").dataset.empty = String(!visible.length);
  if (announce) $("activity-announcement").textContent = announce;
  setActivityCollapsed(activityCollapsed);
  renderRemixProgress(jobs);
}
async function pollActivity() {
  try {
    const snapshot = await api("/api/activity", undefined, 5000);
    activityAvailable = true;
    if (Number.isFinite(snapshot.server_time)) activityClockOffset = snapshot.server_time * 1000 - Date.now();
    const signature = activityStateSignature(snapshot.jobs || []), changed = activitySignature !== signature;
    activitySignature = signature; state.jobs = snapshot.jobs || [];
    $("activity-connection").hidden = true; renderActivity();
    if (changed) { await refresh({ onlyChanged: true }); }
    applyAppUpdateWhenIdle();
  } catch { $("activity-connection").hidden = false; }
}
function scheduleActivityPoll(delay = (state.jobs || []).some(activityIsRunning) ? 750 : 2000) {
  if (activityTimer !== null) clearTimeout(activityTimer);
  activityTimer = setTimeout(async () => {
    activityTimer = null;
    try { await pollActivity(); } finally { scheduleActivityPoll(); }
  }, delay);
}
$("activity-toggle").addEventListener("click", () => setActivityCollapsed(!activityCollapsed));
function appBuildFromHtml(html) {
  return html.match(/<meta name="football-edits-build" content="([a-f0-9]{64})">/)?.[1] || null;
}
async function checkAppBuild({ request = fetch, currentVersion = APP_BUILD, now = Date.now() } = {}) {
  if (!currentVersion || appBuildCheckRunning || (lastAppBuildCheck !== null && now - lastAppBuildCheck < 30000)) return;
  appBuildCheckRunning = true; lastAppBuildCheck = now;
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await request(`/?ui-build-check=${now}`, { cache: "no-store", signal: controller.signal });
    if (!response.ok) return;
    const version = appBuildFromHtml(await response.text());
    if (!version) return;
    availableAppBuild = version === currentVersion ? null : version;
    $("app-update-notice").hidden = !availableAppBuild;
    applyAppUpdateWhenIdle();
  } catch { /* A build check never interrupts playback or generation tracking. */ }
  finally { clearTimeout(timer); appBuildCheckRunning = false; }
}
function applyAppUpdateWhenIdle() {
  const focused = document.activeElement;
  if (!availableAppBuild || typeof window.location?.replace !== "function"
      || player.paused !== true || $("output-player").paused !== true || $("search-preview").open
      || /^(INPUT|TEXTAREA|SELECT)$/.test(focused?.tagName || "") || focused?.isContentEditable
      || pendingGenerations.size || activeLocalJobs || (state.jobs || []).some(activityIsRunning)
      || liveSession || falKeyBusy || $("fal-key").value?.length || reactorKeyBusy || $("reactor-key").value?.length
      || state.projects.some((source) => source.status === "preparing_preview" || (source.generations || []).some(activityIsRunning))
      || [...actionLabelQueues.values()].some((queue) => queue.active || queue.next)
      || [...(document.querySelectorAll?.("video") || [])].some((video) => !video.paused)) return false;
  updateApp();
  return true;
}
function updateApp() {
  if (!availableAppBuild || $("update-app").disabled || typeof window.location?.replace !== "function") return;
  const output = $("output-player");
  const view = { source_id: projectId, active_id: activeId, source_time: player.currentTime,
    source_playing: !player.paused, source_frozen: !$("freeze-preview").hidden,
    output_id: viewedOutputs.get(projectId), output_time: output.currentTime,
    output_playing: !output.paused, saved_at: Date.now() };
  try { sessionStorage.setItem(UPDATE_VIEW_STORAGE_KEY, JSON.stringify(view)); } catch { /* Existing project and run tracking still persist separately. */ }
  rememberProject(); $("update-app").disabled = true;
  const url = new URL(window.location.href); url.searchParams.set("ui", availableAppBuild);
  window.location.replace(url.href);
}
function restoreUpdateView() {
  let view;
  try { view = JSON.parse(sessionStorage.getItem(UPDATE_VIEW_STORAGE_KEY)); sessionStorage.removeItem(UPDATE_VIEW_STORAGE_KEY); } catch { return; }
  if (!view || view.source_id !== projectId || Date.now() - view.saved_at > 10 * 60 * 1000) return;
  activeId = project()?.candidates?.some((item) => item.id === view.active_id) ? view.active_id : null;
  if (finishedClips().some((clip) => clip.id === view.output_id)) viewedOutputs.set(projectId, view.output_id);
  render();
  const restorePlayer = (video, time, playing) => {
    if (!Number.isFinite(time) || time < 0) return;
    const restore = () => {
      video.currentTime = Math.min(time, Number.isFinite(video.duration) ? Math.max(0, video.duration - .01) : time);
      if (playing) video.play().catch(() => {});
    };
    if (video.readyState >= 1) restore(); else video.addEventListener("loadedmetadata", restore, { once: true });
  };
  restorePlayer(player, view.source_time, view.source_playing);
  if ($("output-player").dataset.output === view.output_id) restorePlayer($("output-player"), view.output_time, view.output_playing);
  if (view.source_frozen && active()?.frame_url) {
    $("freeze-preview").src = active().frame_url; $("freeze-preview").hidden = Boolean(view.source_playing);
  }
}
function sourcePresentation(source) {
  const original = source.media, preview = source.preview?.media || original;
  const dimensions = (media) => `${media.width} × ${media.height}`;
  const compatiblePreview = !!source.preview?.kind && source.preview.kind !== "original";
  const playbackUrl = source.preview_url || source.preview?.url || source.video_url || null;
  const preparing = source.status === "preparing_preview" || !playbackUrl;
  const resolution = original.height === 2160 && original.width >= 3840 ? "4K" : `${original.height}p`;
  const preparationLabel = `Preparing ${resolution} playback`;
  return {
    originalLabel: `${source.source_quality?.selection === "best_available" ? "Highest available original" : "Imported original"} · ${dimensions(original)}`,
    previewLabel: preparing ? preparationLabel : compatiblePreview ? `Browser preview · ${dimensions(preview)}` : "",
    note: preparing ? "The original is saved. A full-resolution browser preview is being prepared."
      : compatiblePreview ? "Playback uses a compatible preview. Freeze frames are extracted from the original." : "Playback and freeze-frame extraction use the imported original.",
    playbackUrl: preparing ? null : playbackUrl,
    originalUrl: source.original_url || source.video_url,
    compatiblePreview, preparing, preparationLabel,
  };
}
function setSourcePlayback(source) {
  const presentation = sourcePresentation(source), sameSource = player.dataset.source === source.id;
  const preparation = $("source-playback-status");
  preparation.hidden = !presentation.preparing;
  preparation.textContent = presentation.preparing ? `${presentation.preparationLabel}\nThe original is saved. Playback will appear here automatically.` : "";
  player.controls = !presentation.preparing;
  if (!presentation.playbackUrl) {
    player.pause();
    if (player.getAttribute("src")) { player.removeAttribute("src"); player.load(); }
    player.dataset.source = source.id; activeId = null; $("freeze-preview").hidden = true;
    return;
  }
  if (sameSource && player.getAttribute("src") === presentation.playbackUrl) return;
  const time = player.currentTime, wasPlaying = !player.paused;
  player.pause(); player.src = presentation.playbackUrl; player.dataset.source = source.id;
  if (!sameSource) { activeId = null; $("freeze-preview").hidden = true; return; }
  player.addEventListener("loadedmetadata", () => {
    if (player.dataset.source !== source.id || player.getAttribute("src") !== presentation.playbackUrl) return;
    if (Number.isFinite(time)) player.currentTime = Math.min(time, Number.isFinite(player.duration) ? Math.max(0, player.duration - .01) : time);
    if (wasPlaying) player.play().catch(() => {});
  }, { once: true });
}
function render() {
  renderOrbitChoices();
  const p = project();
  if (!p) { renderEmptyWorkspace(); return; }
  $("video-title").textContent = p.title;
  $("source-detail").textContent = `${Math.round(p.media.duration / 60)} min`;
  const presentation = sourcePresentation(p);
  $("analyze").disabled = presentation.preparing;
  $("cutups-details").hidden = isManualSource(p);
  $("moment-picker").hidden = isManualSource(p);
  $("source-original-resolution").textContent = presentation.originalLabel;
  $("source-preview-resolution").textContent = presentation.previewLabel;
  $("source-preview-resolution").hidden = !presentation.previewLabel;
  $("source-quality-note").textContent = presentation.note;
  $("source-original-link").href = presentation.originalUrl;
  setSourcePlayback(p);
  const candidates = p.candidates || [];
  renderFramePicker(p, presentation.preparing);
  $("candidate-count").textContent = candidates.length;
  const list = $("candidates"); list.replaceChildren();
  for (const item of candidates) {
    const row = node("div", undefined, `candidate${item.id === activeId ? " active" : ""}`);
    const button = node("button", undefined, "candidate-preview"); button.type = "button";
    button.disabled = presentation.preparing;
    const title = momentTitle(item, p);
    button.append(node("small", clock(item.freeze_time ?? item.time)), node("strong", title), node("span", `${title !== item.label ? item.label + " · " : ""}${item.cue || ""}`));
    button.addEventListener("click", () => choose(item.id));
    const keep = node("input"); keep.type = "checkbox"; keep.checked = item.selected;
    keep.setAttribute("aria-label", `Keep ${item.label} at ${clock(item.time)}`);
    keep.addEventListener("change", () => safely(async () => {
      await api("/api/select", { video_id: p.id, item_id: item.id, selected: keep.checked });
      item.selected = keep.checked; renderSelection();
    }));
    row.append(button, keep); list.append(row);
  }
  if (!candidates.length) list.append(node("p", presentation.preparing ? "The original is imported. Cut-ups will appear after playback preparation and analysis." : "Analyze this video to find candidate cut-ups.", "empty"));
  renderSelection(); drawWave();
  if (active()) renderActive();
  else {
    for (const id of ["scrub", "step-back", "step-forward", "play-cut", "set-frame"]) $(id).disabled = true;
    $("clip-title").textContent = presentation.preparing ? presentation.preparationLabel : "Choose a cut-up";
    $("cue-text").textContent = presentation.preparing ? "Your imported video is being prepared for playback and analysis." : "Select a suggested moment to review the action.";
    $("cue-kind").textContent = "";
    $("clip-detection").textContent = ""; $("clip-detection").hidden = true;
    $("frame-detail").textContent = "";
  }
}
function renderEmptyWorkspace() {
  for (const video of [player, $("output-player"), $("remix-player")]) {
    video.pause(); video.removeAttribute("src"); video.load(); delete video.dataset.source; delete video.dataset.output;
  }
  $("freeze-preview").hidden = true; $("freeze-preview").removeAttribute("src");
  for (const id of ["analyze", "scrub", "step-back", "step-forward", "play-cut", "set-frame", "generate", "export", "rerun-output", "combine-finished"]) $(id).disabled = true;
  for (const id of ["candidates", "selected-list", "generations", "exports", "output-picker", "moment-select"]) $(id).replaceChildren();
  for (const id of ["output-workbench", "output-run-status", "newest-output", "cutups-details", "moment-picker", "source-preview-resolution"]) $(id).hidden = true;
  $("output-empty").hidden = false;
  $("source-playback-status").hidden = false; $("source-playback-status").textContent = "Find a video to begin.";
  $("video-title").textContent = "Your reference video"; $("clip-title").textContent = "Choose a video above.";
  $("generation-frame").textContent = "Choose a video and save a freeze frame.";
  $("frame-detail").textContent = "Play your video, then pause on the moment you want.";
  for (const id of ["source-original-resolution", "source-detail", "source-quality-note", "clip-detection", "cue-text", "cue-kind", "output-summary", "output-resolution", "output-quality", "selection-audio", "selection-duration", "artifact-count", "status"]) $(id).textContent = "";
  $("source-original-link").removeAttribute("href");
  $("timecode").textContent = "00:00.000"; $("selected-count").textContent = "0"; $("candidate-count").textContent = "0"; $("output-count").textContent = "";
  renderRemixes({ empty: true });
}
function renderSelection() {
  const p = project(); if (!p) return;
  const selected = (p.candidates || []).filter((c) => c.selected);
  $("selected-count").textContent = selected.length;
  const exact = selected.every((item) => Number.isFinite(item.edit_window?.final_seconds));
  const seconds = selected.reduce((total, item) => total + (item.edit_window?.final_seconds
    ?? state.defaults.lead_seconds + state.defaults.orbit_seconds + state.defaults.tail_seconds), 0);
  $("selection-duration").textContent = selected.length ? `${exact ? "" : "Up to "}${formatSeconds(seconds)}s planned finished footage` : "";
  $("export").disabled = !selected.length;
  const runs = p.generations || [];
  const running = runs.some((r) => ["queued", "running"].includes(r.status));
  const frame = active();
  $("generate").disabled = p.generation_available === false || p.status === "preparing_preview" || !state.generation.ready || running || pendingGenerations.has(projectId) || !frame?.frame_url;
  $("generation-frame").textContent = frame?.frame_url ? `Saved frame · ${clock(frame.freeze_time)}` : "Pause the video and use a frame above.";
  $("selection-audio").textContent = p.audio_mode === "source_slow_motion" ? "Automatic audio warp: music and sound slow down, loop through the generated section, then return to normal speed." : "";
  $("selection-audio").hidden = !$("selection-audio").textContent;
  updateTiming();
  const generations = $("generations"); generations.replaceChildren();
  const falRuns = runs.filter((r) => (r.provider === "Fal" && r.status === "complete" && r.url) || r.composites?.length);
  const testNumbers = new Map([...falRuns].reverse().map((r, i) => [r.id, i + 1]));
  const latestFal = falRuns.find((r) => r.url);
  $("artifact-count").textContent = `${runs.length} clips · ${(p.exports || []).length} downloads`;
  for (const run of runs) {
    const row = node("details", undefined, "generation-result");
    row.open = run.id === latestFal?.id;
    const name = testNumbers.has(run.id) ? `Clip ${testNumbers.get(run.id)}` : run.kind;
    row.append(node("summary", `${name} · ${clock(run.freeze_time)} · ${run.status}`));
    if (run.url) {
      const video = node("video"); video.dataset.source = run.url; video.controls = true; video.playsInline = true; video.preload = "none";
      const link = node("a", run.provider === "Fal" ? "Download generated clip ↓" : "Download generated clip ↓"); link.href = run.url; link.download = `${run.id}.mp4`;
      row.append(video, link);
      row.addEventListener("toggle", updateArtifactPlayers);
      if (run.audio === "silent_test") row.append(node("p", "Silent generated clip. Crowd audio and resumed source action are not included.", "quiet"));
      if (run.provider === "Fal") {
        const controls = node("div", undefined, "output-controls");
        for (const tail of [state.defaults.tail_seconds]) {
          const existing = (run.composites || []).find((c) => c.tail_seconds === tail);
          if (existing) continue;
          const button = node("button", `Assemble with ${tail}s continuation`, "secondary");
          button.addEventListener("click", () => safely(async () => {
            button.disabled = true;
            try {
              await job("/api/assemble", { video_id: p.id, run_id: run.id, tail_seconds: tail }, "Joining original action with the saved generated clip…");
              await refresh(); status("Your assembled highlight is ready under Outputs.");
            } finally { button.disabled = false; }
          }));
          controls.append(button);
        }
        row.append(controls);
      }
    } else if (run.error) row.append(node("p", run.error, "quiet"));
    generations.append(row);
  }
  if (!runs.length) generations.append(node("p", "No generated clips yet.", "quiet"));
  updateArtifactPlayers();
  const list = $("selected-list"); list.replaceChildren();
  for (const item of selected) {
    const chip = node("div", undefined, "selection-chip");
    chip.append(node("span", clock(item.freeze_time ?? item.time)), node("b", item.label)); list.append(chip);
  }
  if (!selected.length) list.append(node("p", "Keep a cut-up to add it here.", "quiet"));
  const exports = $("exports"); exports.replaceChildren();
  for (const result of p.exports || []) {
    const row = node("div", undefined, "export-item");
    const link = node("a", `${result.count} source clips ↓`); link.href = result.url; row.append(link);
    if (result.reel_url) { const reel = node("a", "Source reel ↗"); reel.href = result.reel_url; reel.target = "_blank"; row.append(reel); }
    exports.append(row);
  }
  if (!(p.exports || []).length) exports.append(node("p", "No source downloads prepared yet.", "quiet"));
  renderOutputs();
}
function updateArtifactPlayers() {
  for (const video of $("generations").querySelectorAll("video")) {
    if ($("artifact-history").open && video.parentElement.open) {
      if (!video.getAttribute("src")) video.src = video.dataset.source;
    } else video.pause();
  }
}
function finishedClips() {
  const runs = project()?.generations || [];
  const tests = [...runs].reverse().filter((r) => (r.provider === "Fal" && r.status === "complete" && r.url) || r.composites?.length);
  const numbers = new Map(tests.map((r, i) => [r.id, i + 1]));
  return runs.flatMap((run) => [...(run.composites || [])].reverse().map((clip) => ({
    ...clip, run_id: clip.run_id ?? run.id, freeze_time: clip.source_window?.freeze_time ?? run.freeze_time,
    orbit_url: run.url, raw_orbit_url: run.raw_url, test_number: numbers.get(run.id), motion_review: run.motion_review,
    native_generation_media: run.native_media, orbit_path: run.orbit_path, orbit_path_label: runOrbitLabel(run),
    action_title: actionTitle(run.action_label, run.freeze_time, project()?.media?.fps)
      || actionTitle(project()?.candidates?.find((item) => item.id === run.item_id)?.action_label, run.freeze_time, project()?.media?.fps),
  })));
}
function outputLabel(clip) {
  return `Clip ${clip.test_number}${clip.orbit_path_label ? ` · ${clip.orbit_path_label}` : ""}${clip.action_title ? ` · ${clip.action_title}` : ""} · source ${clock(clip.freeze_time)} · ${Math.round(clip.media.duration)}s${clip.transition === "cut" ? " · direct cut" : ""}`;
}
function outputResolution(clip, source = project()) {
  const provenance = clip.resolution_provenance || {};
  const valid = (size) => Array.isArray(size) && size.length === 2 && size.every((value) => Number.isFinite(value) && value > 0);
  const label = (size) => size[1] === 2160 && size[0] >= 3840 ? "4K" : `${size[1]}p`;
  const original = provenance.source_size || [source?.media?.width, source?.media?.height];
  const generated = provenance.generated_size || [clip.native_generation_media?.width, clip.native_generation_media?.height];
  const exported = [clip.media?.width, clip.media?.height];
  return [valid(original) ? `Source ${label(original)}` : "", valid(generated) ? `Generated ${label(generated)}` : "",
    valid(exported) ? `Export ${label(exported)}${provenance.generated_orbit_upscaled ? " (generated section upscaled)" : ""}` : ""].filter(Boolean).join(" · ");
}
function newestFinishedOutput(snapshot = state) {
  const timestamp = (value) => {
    const number = typeof value === "number" ? (value < 1e12 ? value * 1000 : value) : Date.parse(value);
    return Number.isFinite(number) ? number : null;
  };
  const available = [];
  for (const source of snapshot.projects) {
    for (const run of source.generations || []) {
      if (run.status !== "complete") continue;
      const clip = [...(run.composites || [])].reverse().find((item) => item.id && item.url && item.media?.duration);
      if (!clip) continue;
      const jobTimes = (snapshot.jobs || []).filter((job) => (job.run_id && job.run_id === run.id) || (run.job_id && job.id === run.job_id))
        .map((job) => timestamp(job.created_at)).filter((value) => value !== null);
      const created = timestamp(run.created_at) ?? (jobTimes.length ? Math.max(...jobTimes) : null);
      available.push({ source, run, clip, created });
    }
  }
  const dated = available.filter((item) => item.created !== null);
  return dated.length ? dated.sort((a, b) => b.created - a.created)[0] : available[0];
}
function openFinishedOutput(output) {
  if (!output) return;
  projectId = output.source.id; activeId = null; rememberProject(); render();
  viewedOutputs.set(projectId, output.clip.id); renderOutputViewer();
  $("source-select").value = projectId;
  $("outputs").scrollIntoView({ block: "start", behavior: "smooth" });
}
function renderNewestOutput() {
  const output = newestFinishedOutput(), button = $("newest-output");
  button.hidden = !output;
  if (!output) return;
  const label = `${output.created === null ? "Recent output" : "Newest output"} · ${output.source.title || output.source.id} · ${Math.round(output.clip.media.duration)}s`;
  if (button.dataset.source === output.source.id && button.dataset.output === output.clip.id && button.title === label) return;
  button.replaceChildren(node("span", label), node("span", "View →", "newest-output-action"));
  button.title = label;
  button.dataset.source = output.source.id; button.dataset.output = output.clip.id;
}
function runProgress(source, run, jobs = state.jobs || [], now = Date.now()) {
  const bound = jobs.filter((job) => (job.run_id && job.run_id === run.id) || (run.job_id && job.id === run.job_id));
  const runningJob = bound.find((job) => ["queued", "running"].includes(job.status));
  const failed = ["failed", "interrupted"].includes(run.status)
    || (!runningJob && bound.some((job) => ["failed", "interrupted"].includes(job.status)));
  const active = !failed && (["queued", "running"].includes(run.status) || !!runningJob);
  const stage = run.stage || runningJob?.stage || (run.status === "complete" ? "assembling" : run.status === "queued" ? "queued" : "generating");
  const stages = {
    queued: "Waiting for the run to start.",
    generating: "Generating with world models.",
    downloading: "Downloading the generated video.",
    normalizing: "Preparing the video format.",
    anchoring: "Matching the saved reference at the orbit endpoints.",
    assembling: "Joining source action and preparing audio.",
  };
  const created = run.created_at ?? runningJob?.created_at ?? bound[0]?.created_at;
  let started = typeof created === "number" ? (created < 1e12 ? created * 1000 : created) : Date.parse(created);
  if (!Number.isFinite(started)) started = pendingGenerations.get(source.id)?.started_at;
  const seconds = Number.isFinite(started) ? Math.max(0, Math.floor((now - started) / 1000)) : null;
  const elapsed = seconds === null ? "" : seconds < 60 ? `${seconds}s elapsed` : `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s elapsed`;
  return { active, failed, stage, elapsed,
    title: failed ? "Latest run · needs attention" : active ? "New run · processing" : "Generated clip ready · highlight pending",
    message: failed ? run.error || bound.find((job) => job.error)?.error || "The run stopped before its finished highlight was ready. Its saved frame is preserved."
      : active ? stages[stage] || "Preparing this run's video."
        : "The generated clip is saved. Its finished highlight is not available yet.",
  };
}
function renderOutputRunStatus() {
  const container = $("output-run-status"); container.replaceChildren();
  for (const source of state.projects) {
    const runs = (source.generations || []).filter((run) => run.provider === "Fal");
    for (const [index, run] of runs.entries()) {
      const progress = runProgress(source, run);
      const local = source.id === projectId;
      if (!progress.active && !(local && index === 0 && (progress.failed || (run.url && !run.composites?.length)))) continue;
      const card = node("div", undefined, `run-status-card${progress.failed ? " run-needs-attention" : ""}`);
      card.dataset.run = run.id; card.dataset.stage = progress.stage;
      const reference = node("a"); reference.href = run.frame_url || `/media/${encodeURIComponent(source.id)}/exports/${encodeURIComponent(run.id)}/frame.png`;
      reference.target = "_blank"; reference.rel = "noopener"; reference.className = "run-reference";
      const thumbnail = node("img"); thumbnail.src = reference.href;
      thumbnail.alt = `Saved source frame at ${clock(run.freeze_time)}`;
      thumbnail.addEventListener("error", () => { thumbnail.hidden = true; });
      reference.append(thumbnail, node("span", "View saved frame ↗"));
      const detail = node("div", undefined, "run-status-detail");
      const title = node("h4", local ? progress.title : `${progress.title} in another video`);
      title.setAttribute("aria-live", "polite");
      detail.append(title, node("p", progress.message), node("p", `Source frame ${clock(run.freeze_time)}${runOrbitLabel(run) ? ` · ${runOrbitLabel(run)}` : ""}${progress.elapsed ? ` · ${progress.elapsed}` : ""}`, "output-summary"));
      if (!local) {
        const button = node("button", `Open ${source.title || source.id}`, "secondary run-project-button");
        button.type = "button";
        button.addEventListener("click", () => {
          projectId = source.id; activeId = null; rememberProject(); render(); $("source-select").value = source.id;
          $("outputs").scrollIntoView({ behavior: "smooth", block: "start" });
        });
        detail.append(button);
      } else if (run.url) {
        const orbit = node("a", "View generated clip ↗"); orbit.href = run.raw_url || run.url; orbit.target = "_blank"; orbit.rel = "noopener";
        detail.append(orbit);
      }
      card.append(reference, detail); container.append(card);
    }
  }
  container.hidden = !container.children.length;
}
function updateReelSelection(clips = finishedClips()) {
  const count = clips.filter((clip) => selectedOutputs.has(clip.id)).length;
  $("combine-finished").disabled = !count;
  $("reel-selection-summary").textContent = `${count} of ${clips.length} highlights included, in the order shown below.`;
}
function renderOutputViewer(clips = finishedClips()) {
  const clip = clips.find((item) => item.id === viewedOutputs.get(projectId));
  if (!clip) return;
  const index = clips.indexOf(clip), video = $("output-player");
  $("output-picker").value = clip.id;
  $("output-prev").disabled = index === 0;
  $("output-next").disabled = index === clips.length - 1;
  $("rerun-output").disabled = !state.generation.ready || !clip.run_id || pendingGenerations.has(projectId)
    || (project()?.generations || []).some((run) => ["queued", "running"].includes(run.status));
  if (video.dataset.output !== clip.id || video.getAttribute("src") !== clip.url) {
    video.pause(); video.src = clip.url; video.dataset.output = clip.id;
  }
  video.setAttribute("aria-label", outputLabel(clip));
  $("output-title").textContent = `Clip ${clip.test_number}${clip.orbit_path_label ? ` · ${clip.orbit_path_label}` : ""} · ${clip.action_title || `${Math.round(clip.media.duration)}s highlight`}${clip.transition === "cut" ? " · direct cut" : ""}`;
  const segments = clip.segments || [];
  const duration = (kind, fallback) => {
    const segment = segments.find((item) => item.kind === kind);
    return segment ? segment.output_end - segment.output_start : fallback;
  };
  const lead = duration("source_lead", clip.lead_seconds ?? state.defaults.lead_seconds);
  const orbit = duration("camera_orbit", clip.provider_orbit_seconds ?? state.defaults.orbit_seconds);
  const tail = duration("source_tail", clip.actual_tail_seconds ?? clip.source_window?.actual_tail_seconds ?? clip.tail_seconds);
  $("output-summary").textContent = `Source freeze ${clock(clip.freeze_time)} · ${formatSeconds(lead)}s original action + ${formatSeconds(orbit)}s generated moment + ${formatSeconds(tail)}s continued play`;
  $("output-resolution").textContent = outputResolution(clip);
  $("output-resolution").hidden = !$("output-resolution").textContent;
  $("output-return").textContent = clip.reference_closure?.first_last_pixel_hash_equal
    ? "The edited clip has matching start and end reference frames, verified in this finished video."
    : "No matching reference endpoint check is recorded for this version.";
  $("output-transition").textContent = clip.transition === "cut"
    ? clip.join_version === "cut_next_frame_v1" ? "Direct cut; action resumes on the next source frame." : "Direct cut."
    : "";
  $("output-transition").hidden = !$("output-transition").textContent;
  $("output-quality").textContent = displayText(clip.motion_review?.note);
  $("output-quality").hidden = !clip.motion_review?.note;
  $("output-download").href = clip.url;
  $("output-download").download = `${clip.id}.mp4`;
  $("output-orbit").hidden = !clip.orbit_url;
  if (clip.orbit_url) $("output-orbit").href = clip.orbit_url;
  $("output-generated").hidden = !clip.raw_orbit_url;
  if (clip.raw_orbit_url) $("output-generated").href = clip.raw_orbit_url;
  $("output-previous").hidden = !clip.previous_edit_url;
  if (clip.previous_edit_url) $("output-previous").href = clip.previous_edit_url;
  else $("output-previous").removeAttribute("href");
  const start = clip.source_window?.start ?? Math.max(0, clip.freeze_time - lead);
  const end = clip.source_window?.end ?? clip.freeze_time + tail;
  const sourceView = sourcePresentation(project());
  $("output-source").hidden = !sourceView.playbackUrl;
  if (sourceView.playbackUrl) $("output-source").href = `${sourceView.playbackUrl}#t=${start},${end}`;
  else $("output-source").removeAttribute("href");
  $("output-source").textContent = sourceView.compatiblePreview ? "View source preview ↗" : "View original source ↗";
  const audio = clip.audio_provenance?.orbit;
  const sourceSound = clip.audio_provenance?.source_audio ?? clip.audio_provenance?.crowd_bed;
  const silentSound = Boolean(sourceSound?.silence_reason)
    || ["silent", "no_audio_stream", "audio_ended"].includes(sourceSound?.source_window_state);
  $("output-audio").textContent = audio === "source_slow_motion_loop"
    ? silentSound ? "The source is silent at this moment, so the generated section stays silent."
      : "Source audio with a slow-motion effect: music and sound slow down, loop through the generated section, then return to normal speed."
    : audio === "silence_no_crowd_recording"
    ? "Original audio before and after the generated section. The generated section is silent; crowd separation is pending."
    : audio === "source_crowd_centre_suppressed"
      ? "Processed source crowd atmosphere during the generated section, with commentary reduced. Original commentary resumes with continued play."
    : audio === "source_crowd_fallback"
      ? "Reused stadium ambience from another source during the generated section. Original commentary resumes with continued play."
    : audio === "supplied_crowd_recording"
      ? "Original audio before and after the generated section, with a supplied crowd bed underneath."
      : "Original audio before and after the generated section. Review the generated section audio in this output.";
}
function renderOutputs() {
  renderNewestOutput();
  renderOutputRunStatus();
  const p = project(), clips = finishedClips();
  const newClips = clips.filter((clip) => !knownOutputs.has(clip.id));
  if (newClips.length || !clips.some((clip) => clip.id === viewedOutputs.get(projectId))) {
    viewedOutputs.set(projectId, (newClips[0] || clips[0])?.id);
  }
  const picker = $("output-picker"), includes = $("output-includes");
  picker.replaceChildren(); includes.replaceChildren();
  $("output-count").textContent = `${clips.length} finished ${clips.length === 1 ? "highlight" : "highlights"}`;
  $("output-empty").hidden = !!clips.length;
  $("output-workbench").hidden = !clips.length;
  if (!clips.length) { $("output-player").pause(); $("output-player").removeAttribute("src"); delete $("output-player").dataset.output; }
  for (const clip of clips) {
    if (!knownOutputs.has(clip.id)) { knownOutputs.add(clip.id); selectedOutputs.add(clip.id); }
    const option = node("option", outputLabel(clip)); option.value = clip.id; picker.append(option);
    const label = node("label", undefined, "check-label");
    const check = node("input"); check.type = "checkbox"; check.checked = selectedOutputs.has(clip.id);
    check.addEventListener("change", () => {
      if (check.checked) selectedOutputs.add(clip.id); else selectedOutputs.delete(clip.id);
      updateReelSelection();
    });
    label.append(check, node("span", outputLabel(clip))); includes.append(label);
  }
  renderOutputViewer(clips); updateReelSelection(clips);
  const reels = $("output-reels"); reels.replaceChildren();
  for (const reel of p?.reels || []) {
    const row = node("div", undefined, "reel-links"), link = node("a", `Combined highlights · ${Math.round(reel.media.duration)}s ↓`);
    link.href = reel.url; link.download = `${reel.id}.mp4`; row.append(link); reels.append(row);
  }
  renderRemixes();
}
function remixOptions(select, options, selected) {
  const signature = JSON.stringify(options);
  if (select.dataset.options !== signature) {
    select.replaceChildren();
    for (const item of options) {
      const option = node("option"); option.textContent = remixText(item.label); option.value = item.id; select.append(option);
    }
    select.dataset.options = signature;
  }
  select.value = selected || "";
}
function remixText(value) {
  return String(value ?? "").replace(/\b(?:FAL_KEY|REACTOR_API_KEY)\b/gi, "the connection");
}
function remixStatus(message) { $("status").textContent = remixText(message); }
function remixClips() {
  return finishedClips().filter((clip) => clip.id && clip.url && Number.isFinite(clip.media?.duration));
}
function remixDraft() {
  if (!projectId) return null;
  if (!remixDrafts.has(projectId)) remixDrafts.set(projectId, { source: "__all__", preset_id: null, prompt: "", message: "" });
  return remixDrafts.get(projectId);
}
async function loadRemixPresets() {
  if (remixCatalogLoading) return;
  remixCatalogLoading = true; remixCatalogError = ""; renderRemixes();
  try {
    const value = await api("/api/remix-presets", undefined, 15000);
    if (!Array.isArray(value.presets) || !value.presets.length || value.presets.some((item) =>
      typeof item.id !== "string" || typeof item.label !== "string" || typeof item.prompt !== "string")) {
      throw new Error("Reactor presets are unavailable.");
    }
    remixCatalog = value;
  } catch (error) {
    remixCatalogError = error.status === 404 ? "Reactor remix is not available on this server yet." : error.message;
  } finally { remixCatalogLoading = false; renderRemixes(); }
}
function renderRemixViewer(remixes = project()?.remixes || []) {
  const remix = remixes.find((item) => item.id === viewedRemixes.get(projectId) && item.status === "complete" && item.url);
  if (!remix) return;
  const video = $("remix-player");
  if (video.dataset.output !== remix.id || video.getAttribute("src") !== remix.url) {
    video.pause(); video.src = remix.url; video.dataset.output = remix.id;
  }
  $("remix-result").value = remix.id;
  const title = remixText(remix.label || "Reactor remix");
  video.setAttribute("aria-label", title); $("remix-title").textContent = title;
  $("remix-download").href = remix.url; $("remix-download").download = `${remix.id}.mp4`;
  const original = remixClips().find((clip) => clip.id === remix.composite_id);
  $("remix-summary").textContent = [
    [remixCatalog?.provider || "Reactor", remixCatalog?.model].filter(Boolean).join(" "),
    Number.isFinite(remix.media?.duration) ? `${Number(remix.media.duration.toFixed(2))}s` : "",
    Number.isFinite(remix.media?.height) ? `${remix.media.height}p output` : "",
    original ? `From clip ${original.test_number} · source ${clock(original.freeze_time)}` : "",
  ].filter(Boolean).join(" · ");
}
function livePrompt(mode = liveLook) {
  const prompt = remixCatalog?.live_prompts?.[mode];
  return typeof prompt === "string" && prompt.trim() ? prompt : null;
}
function liveSessionLimit() {
  const seconds = Number(remixCatalog?.live_max_session_seconds);
  return Number.isFinite(seconds) && seconds > 0 ? seconds : null;
}
function liveLimitLabel(seconds) {
  return seconds % 60 === 0 ? `${seconds / 60} minutes` : `${seconds} seconds`;
}
function setLiveDeadline(session, seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0 || (session.maxSeconds && seconds >= session.maxSeconds)) return;
  session.maxSeconds = seconds;
  if (session.timer) clearTimeout(session.timer);
  session.timer = setTimeout(() => {
    if (liveSession === session) stopLivePreview(`Live preview stopped at the ${liveLimitLabel(session.maxSeconds)} limit.`);
  }, Math.max(0, session.startedAt + seconds * 1000 - Date.now()));
}
function tickLiveElapsed(session) {
  if (liveSession !== session) return;
  renderLivePreview();
  if (liveSession !== session) return;
  session.clockTimer = setTimeout(() => tickLiveElapsed(session), 1000);
}
function renderLivePreview({ empty = false } = {}) {
  if (liveSession && (empty || liveSession.sourceId !== projectId)) {
    stopLivePreview("Live preview stopped when the source changed."); return;
  }
  const clips = empty ? [] : remixClips();
  if (!clips.some((clip) => clip.id === liveClipSelections.get(projectId))) {
    liveClipSelections.set(projectId, clips.find((clip) => clip.id === viewedOutputs.get(projectId))?.id || clips[0]?.id);
  }
  remixOptions($("reactor-live-source"), clips.length ? clips.map((clip) => ({ id: clip.id, label: outputLabel(clip) }))
    : [{ id: "", label: "Finish a clip above to begin" }], liveClipSelections.get(projectId));
  $("reactor-live-source").disabled = !clips.length || !!liveSession;
  $("reactor-live-start").disabled = !!liveSession || !clips.length || !livePrompt() || !liveSessionLimit()
    || !reactorReady() || typeof window.ReactorLive?.create !== "function";
  $("reactor-live-stop").disabled = !liveSession;
  $("reactor-live-audio").disabled = !liveSession;
  for (const look of ["day", "night"]) {
    $("reactor-live-" + look).setAttribute("aria-pressed", String(look === liveLook));
    $("reactor-live-" + look).disabled = !livePrompt(look);
  }
  const labels = { loading_source: "Loading the selected clip", authenticating: "Connecting to Reactor",
    connecting: "Connecting the live stream", streaming_source: "Sending the source video to Reactor",
    generating: liveSession?.hasPicture ? "Waiting for Reactor's next chunk" : "Waiting for Reactor's first live picture", live: "Live from Reactor", stopped: "Live preview stopped" };
  let message = livePreviewState.message || labels[livePreviewState.state] || "Live preview is stopped.";
  if (!liveSession && livePreviewState.state === "idle") {
    message = remixCatalogLoading ? "Loading Reactor settings…" : !reactorReady() ? "Add your Reactor API key above to start a live preview."
      : typeof window.ReactorLive?.create !== "function" ? "The live preview adapter is unavailable. Reload the app to try again."
      : !livePrompt() ? "Live Day/Night prompts are unavailable. Refresh the Reactor connection."
      : !liveSessionLimit() ? "Live session settings are unavailable. Refresh the Reactor connection."
      : !clips.length ? "Choose a finished clip above to continue." : "Ready. Start connects to Reactor.";
  }
  const details = [];
  if (Number.isFinite(livePreviewState.width) && Number.isFinite(livePreviewState.height)) details.push(`${livePreviewState.width} × ${livePreviewState.height}`);
  if (liveSession) details.push(`${durationText(Math.max(0, Date.now() - liveSession.startedAt) / 1000)} elapsed`);
  $("reactor-live-status").textContent = [message, ...details].join(" · ");
  $("reactor-live-prompt-status").textContent = livePromptMessage;
  const limit = liveSession?.maxSeconds || liveSessionLimit();
  $("reactor-live-limit").textContent = `Uses Reactor credits while connected. ${limit ? `Stops after ${liveLimitLabel(limit)}. ` : ""}Look changes affect upcoming chunks.`;
  $("reactor-live-empty").hidden = !!liveSession?.hasPicture;
  $("reactor-live-empty").textContent = liveSession ? labels[livePreviewState.state] || "Waiting for the live picture…"
    : livePreviewState.state === "error" ? "Live preview stopped. Review the message below." : "Choose a clip, then start your live preview.";
}
function stopLivePreview(message = "Live preview stopped.", stateName = "stopped") {
  const session = liveSession;
  liveSession = null;
  if (session?.timer) clearTimeout(session.timer);
  if (session?.clockTimer) clearTimeout(session.clockTimer);
  livePreviewState = { state: stateName, message };
  livePromptMessage = `${liveLook === "day" ? "Day" : "Night"} selected. Start to connect.`;
  if (session?.client) {
    try { Promise.resolve(session.client.stop()).catch(() => {}); } catch { /* Local media cleanup still runs. */ }
  }
  for (const video of [$("reactor-live-source-video"), $("reactor-live-player")]) {
    video.pause(); video.srcObject = null; video.muted = true;
  }
  $("reactor-live-audio").checked = false;
  renderLivePreview();
}
async function startLivePreview() {
  if (liveSession || !reactorReady() || !livePrompt() || !liveSessionLimit() || typeof window.ReactorLive?.create !== "function") return;
  const clip = remixClips().find((item) => item.id === liveClipSelections.get(projectId));
  if (!clip) return;
  const session = { sequence: ++liveSessionSequence, sourceId: projectId, clipId: clip.id, prompt: livePrompt(), client: null,
    timer: null, clockTimer: null, startedAt: Date.now(), maxSeconds: null, hasPicture: false, ready: false };
  liveSession = session; livePreviewState = { state: "loading_source", elapsedMs: 0 };
  livePromptMessage = `${liveLook === "day" ? "Day" : "Night"} requested. Waiting for Reactor.`;
  $("reactor-live-audio").checked = false; $("reactor-live-source-video").muted = true; $("reactor-live-player").muted = true;
  renderLivePreview();
  try {
    session.client = window.ReactorLive.create({
      onStatus: (event) => {
        if (liveSession !== session) return;
        if (event.state === "stopped" || event.state === "error") {
          stopLivePreview(event.message || (event.state === "error" ? "Reactor live preview failed." : "Live preview stopped."), event.state); return;
        }
        if (event.state === "live") session.hasPicture = true;
        setLiveDeadline(session, event.maxSessionSeconds);
        livePreviewState = { ...livePreviewState, ...event, message: event.message }; renderLivePreview();
      },
      onError: (error) => {
        if (liveSession === session) stopLivePreview(error?.message || "Reactor live preview failed.", "error");
      },
      onPromptApplied: (event) => {
        if (liveSession !== session) return;
        const acknowledged = typeof event === "string" ? event : event?.prompt;
        if (acknowledged && acknowledged !== session.prompt) return;
        livePromptMessage = acknowledged
          ? `${liveLook === "day" ? "Day" : "Night"} requested · accepted for an upcoming chunk.`
          : `${liveLook === "day" ? "Day" : "Night"} requested. Reactor accepted a prompt for an upcoming chunk.`;
        renderLivePreview();
      },
    });
    if (liveSession !== session) { await session.client.stop(); return; }
    setLiveDeadline(session, liveSessionLimit());
    tickLiveElapsed(session);
    const initialPrompt = session.prompt;
    await session.client.start({ sourceUrl: clip.url, videoElement: $("reactor-live-player"),
      sourceVideo: $("reactor-live-source-video"), prompt: initialPrompt });
    if (liveSession !== session) return;
    session.ready = true;
    if (session.prompt !== initialPrompt) await session.client.setPrompt(session.prompt);
  } catch (error) {
    if (liveSession === session) stopLivePreview(error?.message || "Could not start Reactor live preview.", "error");
  }
}
async function changeLiveLook(look) {
  if (!["day", "night"].includes(look) || !livePrompt(look) || liveLook === look) return;
  liveLook = look;
  const session = liveSession;
  livePromptMessage = `${look === "day" ? "Day" : "Night"} ${session ? "requested. Waiting for the next chunk." : "selected. Start to connect."}`;
  renderLivePreview();
  if (!session) return;
  session.prompt = livePrompt(look);
  if (!session.ready) return;
  try { await session.client.setPrompt(session.prompt); }
  catch (error) { if (liveSession === session) stopLivePreview(error?.message || "Could not send the requested look to Reactor.", "error"); }
}
$("reactor-live-start").addEventListener("click", startLivePreview);
$("reactor-live-stop").addEventListener("click", () => stopLivePreview());
$("reactor-live-day").addEventListener("click", () => changeLiveLook("day"));
$("reactor-live-night").addEventListener("click", () => changeLiveLook("night"));
$("reactor-live-source").addEventListener("change", () => {
  if (!liveSession) { liveClipSelections.set(projectId, $("reactor-live-source").value); renderLivePreview(); }
});
$("reactor-live-audio").addEventListener("change", () => {
  $("reactor-live-source-video").muted = !liveSession || !$("reactor-live-audio").checked;
});
window.addEventListener("pagehide", () => stopLivePreview("Live preview stopped because the page closed."));
function renderRemixProgress(jobs = state.jobs || [], { empty = false } = {}) {
  const remixes = empty ? [] : project()?.remixes || [];
  const activeJobs = empty ? [] : jobs.filter((job) => job.operation === "remix" && job.video_id === projectId && activityIsRunning(job));
  const running = remixes.filter((item) => ["queued", "running"].includes(item.status));
  const count = Math.max(activeJobs.length, running.length);
  const current = activeJobs.find((item) => item.status === "running") || activeJobs[0];
  const stopped = [...remixes].sort((a, b) => (activityTime(b.created_at) || 0) - (activityTime(a.created_at) || 0))
    .find((item) => ["failed", "interrupted"].includes(item.status) && !remixes.some((later) =>
      later.status === "complete" && later.url && item.composite_id && later.composite_id === item.composite_id
      && later.preset_id === item.preset_id && later.prompt === item.prompt
      && activityTime(item.created_at) !== null && activityTime(later.created_at) !== null
      && activityTime(later.created_at) > activityTime(item.created_at)));
  const draft = empty ? null : remixDrafts.get(projectId);
  let progress = "";
  if (count) {
    const measured = current ? activityPresentation(current) : null;
    progress = [`${count} Reactor ${count === 1 ? "remix is" : "remixes are"} processing.`,
      measured?.label || running[0]?.message || "Waiting for Reactor.",
      measured?.percent === null || measured?.percent === undefined ? "" : `${Math.round(measured.percent)}%`,
      measured?.metrics, measured?.elapsed].filter(Boolean).join(" · ");
    $("remix-generate").disabled = true;
  } else if (!empty && pendingRemixSources.has(projectId)) progress = "Sending your clips to Reactor…";
  else progress = draft?.message || (stopped ? stopped.error || stopped.message || "A Reactor remix stopped before completion." : "");
  $("remix-progress").textContent = remixText(progress); $("remix-progress").hidden = !progress;
}
function renderRemixes({ empty = false } = {}) {
  const clips = empty ? [] : remixClips(), draft = empty ? null : remixDraft();
  const presets = remixCatalog?.presets || [];
  if (draft && presets.length && !presets.some((preset) => preset.id === draft.preset_id)) {
    const preset = presets.find((item) => /^day\s+to\s+night$/i.test(item.label.trim())) || presets[0];
    draft.preset_id = preset.id; draft.prompt = preset.prompt;
  }
  if (draft && draft.source !== "__all__" && !clips.some((clip) => clip.id === draft.source)) draft.source = "__all__";
  remixOptions($("remix-source"), clips.length
    ? [{ id: "__all__", label: `All finished clips (${clips.length})` }, ...clips.map((clip) => ({ id: clip.id, label: outputLabel(clip) }))]
    : [{ id: "", label: "Finish a clip above to begin" }], clips.length ? draft?.source : "");
  remixOptions($("remix-preset"), presets.map((item) => ({ id: item.id, label: item.label })), draft?.preset_id);
  if ($("remix-prompt").value !== (draft?.prompt || "")) $("remix-prompt").value = draft?.prompt || "";
  $("remix-source").disabled = !clips.length;
  $("remix-preset").disabled = !presets.length || !draft;
  $("remix-prompt").disabled = !presets.length || !draft;
  $("remix-model").textContent = remixCatalog?.model || "";
  const remixes = empty ? [] : [...(project()?.remixes || [])].sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0));
  const running = remixes.filter((item) => ["queued", "running"].includes(item.status));
  const pending = pendingRemixSources.has(projectId) || running.length > 0;
  $("remix-generate").disabled = !clips.length || !draft?.prompt.trim() || !draft?.preset_id
    || !reactorReady() || !!remixCatalogError || remixCatalogLoading || pending;
  const count = draft?.source === "__all__" ? clips.length : 1;
  $("remix-connection").textContent = remixCatalogLoading ? "Loading Reactor presets…"
    : remixCatalogError ? remixText(remixCatalogError)
    : !reactorReady() ? "Add your Reactor API key above to generate remixes."
    : !clips.length ? "Choose a finished clip above to continue."
    : `${count} ${count === 1 ? "clip" : "clips"} will be sent to Reactor. Uses Reactor credits.`;
  $("remix-reconnect").hidden = remixCatalogLoading || (!remixCatalogError && !!reactorReady());
  renderRemixProgress(state.jobs || [], { empty });
  const complete = remixes.filter((item) => item.status === "complete" && item.id && item.url);
  const newResults = complete.filter((item) => !knownRemixes.has(`${projectId}:${item.id}`));
  if (newResults.length || !complete.some((item) => item.id === viewedRemixes.get(projectId))) {
    viewedRemixes.set(projectId, (newResults[0] || complete[0])?.id);
  }
  for (const item of complete) knownRemixes.add(`${projectId}:${item.id}`);
  remixOptions($("remix-result"), complete.map((item, index) => ({ id: item.id,
    label: `${remixText(item.label || "Remix")} · ${complete.length - index}${Number.isFinite(item.media?.duration) ? ` · ${Math.round(item.media.duration)}s` : ""}` })), viewedRemixes.get(projectId));
  $("remix-workbench").hidden = !complete.length; $("remix-empty").hidden = !!complete.length || pending;
  if (complete.length) renderRemixViewer(complete);
  else if ($("remix-player").getAttribute("src")) {
    $("remix-player").pause(); $("remix-player").removeAttribute("src"); $("remix-player").load(); delete $("remix-player").dataset.output;
  }
  renderLivePreview({ empty });
}
async function submitRemix(event) {
  event?.preventDefault();
  const sourceId = projectId, draft = remixDraft(), clips = remixClips(), epoch = workspaceResetEpoch;
  if (!draft || !clips.length || !draft.prompt.trim() || !draft.preset_id || !reactorReady()
      || remixCatalogError || pendingRemixSources.has(sourceId)
      || (project()?.remixes || []).some((item) => ["queued", "running"].includes(item.status))) return;
  const chosen = draft.source === "__all__" ? clips.map((clip) => clip.id) : clips.filter((clip) => clip.id === draft.source).map((clip) => clip.id);
  if (!chosen.length) return;
  const request = { video_id: sourceId, preset_id: draft.preset_id, prompt: draft.prompt,
    ...(draft.source === "__all__" ? { composite_ids: chosen } : { composite_id: chosen[0] }) };
  pendingRemixSources.add(sourceId); activeLocalJobs++; draft.message = ""; renderRemixes();
  remixStatus("Sending your clips to Reactor…");
  try {
    const started = await api("/api/remix", request);
    const jobs = started.jobs || [started];
    if (!jobs.length || jobs.some((item) => !item.job_id)) throw new Error("Reactor submission could not be confirmed. Refresh to check saved jobs before trying again.");
    try { await refresh({ onlyChanged: true }); } catch { /* Read-only polling still tracks acknowledged jobs. */ }
    const outcomes = await Promise.all(jobs.map((item) => pollGeneration({ job_id: item.job_id }, {
      pollInterval: localJobPollInterval, isCurrent: () => workspaceResetEpoch === epoch,
      onStatus: (message) => remixStatus(message.startsWith("Reconnecting")
        ? "Reconnecting to your existing Reactor jobs. No new remix has been submitted." : "Reactor is creating your remixes."),
    })));
    if (workspaceResetEpoch !== epoch) return;
    const complete = outcomes.filter((item) => item.status === "complete").length;
    const stopped = outcomes.find((item) => !["complete", "cancelled"].includes(item.status));
    draft.message = stopped ? `${complete} of ${jobs.length} remixes ready. ${stopped.error || "Check the saved Reactor jobs for details."}`
      : `${complete} ${complete === 1 ? "remix is" : "remixes are"} ready below.`;
    await refresh({ onlyChanged: true }); remixStatus(draft.message);
  } catch (error) {
    if (workspaceResetEpoch !== epoch) return;
    draft.message = `${error.message} No automatic retry was sent.`; remixStatus(draft.message);
    try { await refresh({ onlyChanged: true }); } catch { /* Existing jobs remain saved on the server. */ }
  } finally {
    activeLocalJobs--;
    if (workspaceResetEpoch === epoch) { pendingRemixSources.delete(sourceId); renderRemixes(); }
  }
}
$("remix-form").addEventListener("submit", submitRemix);
$("remix-reconnect").addEventListener("click", loadRemixPresets);
$("remix-source").addEventListener("change", () => {
  const draft = remixDraft(); if (draft) { draft.source = $("remix-source").value; renderRemixes(); }
});
$("remix-preset").addEventListener("change", () => {
  const draft = remixDraft(), preset = remixCatalog?.presets.find((item) => item.id === $("remix-preset").value);
  if (draft && preset) { draft.preset_id = preset.id; draft.prompt = preset.prompt; renderRemixes(); }
});
$("remix-prompt").addEventListener("input", () => {
  const draft = remixDraft(); if (draft) { draft.prompt = $("remix-prompt").value; renderRemixes(); }
});
$("remix-result").addEventListener("change", () => { viewedRemixes.set(projectId, $("remix-result").value); renderRemixViewer(); });
$("artifact-history").addEventListener("toggle", updateArtifactPlayers);
$("newest-output").addEventListener("click", () => openFinishedOutput(newestFinishedOutput()));
$("update-app").addEventListener("click", updateApp);
$("output-picker").addEventListener("change", () => {
  viewedOutputs.set(projectId, $("output-picker").value); renderOutputViewer();
});
for (const [id, step] of [["output-prev", -1], ["output-next", 1]]) {
  $(id).addEventListener("click", () => {
    const clips = finishedClips(), index = clips.findIndex((clip) => clip.id === viewedOutputs.get(projectId));
    if (clips[index + step]) { viewedOutputs.set(projectId, clips[index + step].id); renderOutputViewer(clips); }
  });
}
function choose(id) {
  activeId = id; previewEnd = null; player.pause(); $("freeze-preview").hidden = true;
  player.currentTime = active().freeze_time ?? active().time;
  render(); updatePosition(); scheduleActionLabel();
}
function isManualSource(source) { return source?.selection_mode === "manual" || source?.analysis?.mode === "manual"; }
function renderFramePicker(source, preparing) {
  const candidates = source.candidates || [], select = $("moment-select");
  if (!active() && !preparing && candidates.length) {
    const first = candidates.find((item) => item.selected && item.frame_url)
      || (isManualSource(source) ? candidates.find((item) => item.kind === "manual") : null)
      || candidates.find((item) => item.selected) || candidates[0];
    activeId = first.id;
    const position = () => {
      if (projectId !== source.id || activeId !== first.id) return;
      player.currentTime = first.freeze_time ?? first.time; updatePosition();
    };
    if (player.readyState >= 1) position();
    else player.addEventListener("loadedmetadata", position, { once: true });
    scheduleActionLabel(source, first);
  }
  select.replaceChildren();
  for (const item of candidates) {
    const option = node("option", `${clock(item.freeze_time ?? item.time)} · ${momentTitle(item, source)}`);
    option.value = item.id; select.append(option);
  }
  if (!candidates.length) select.append(node("option", preparing ? "Preparing playback…" : "No suggested moments yet"));
  select.value = activeId || ""; select.disabled = preparing || !candidates.length;
}
$("moment-select").addEventListener("change", () => choose($("moment-select").value));
function renderActive() {
  const item = active(), p = project();
  renderActionLabelStatus();
  $("cue-text").textContent = item.cue;
  $("cue-kind").textContent = [...new Set((item.signals || []).map((s) => s.kind === "audio" ? "Audio energy peak" : "Caption cue"))].join(" · ");
  const slider = $("scrub"); slider.min = p.frame_selection?.min_time ?? state.defaults.lead_seconds;
  slider.max = p.frame_selection?.max_time ?? Math.max(Number(slider.min), p.media.duration - 1 / p.media.fps);
  slider.step = 1 / p.media.fps;
  slider.disabled = false;
  for (const id of ["step-back", "step-forward", "play-cut", "set-frame"]) $(id).disabled = false;
  $("tail").replaceChildren();
  const tailOption = node("option", `Up to ${state.defaults.tail_seconds} seconds`); tailOption.value = state.defaults.tail_seconds;
  $("tail").append(tailOption); $("tail").value = state.defaults.tail_seconds;
  updateTiming();
  $("frame-detail").textContent = item.frame_url ? `Saved at ${clock(item.freeze_time)}. Use this frame again to save a different moment.` : "Pause on the frame you want, then choose Use this frame.";
}
function formatSeconds(value) { return Number(Number(value).toFixed(2)); }
function updateTiming() {
  const d = state.defaults, window = active()?.edit_window;
  const exact = Number.isFinite(window?.actual_tail_seconds);
  const tail = exact ? window.actual_tail_seconds : d.tail_seconds;
  const qualifier = exact ? "" : "up to ";
  $("timing-summary").textContent = `${d.lead_seconds}s action + ${d.orbit_seconds}s generated moment + ${qualifier}${formatSeconds(tail)}s resumed action = ${qualifier}${formatSeconds(d.lead_seconds + d.orbit_seconds + tail)}s`;
}
function updatePosition() {
  $("timecode").textContent = clock(player.currentTime);
  if (active()) $("scrub").value = player.currentTime;
  if (previewEnd !== null && player.currentTime >= previewEnd) { player.pause(); previewEnd = null; }
  drawWave();
}
function seek(time) {
  player.pause(); previewEnd = null; $("freeze-preview").hidden = true;
  player.currentTime = Math.max(Number($("scrub").min), Math.min(Number($("scrub").max), time));
  updatePosition();
}
function drawWave() {
  const canvas = $("waveform"), p = project(); if (!p || !canvas.clientWidth) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = canvas.clientWidth * ratio; canvas.height = 68 * ratio;
  const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
  const width = canvas.clientWidth, height = 68;
  const css = getComputedStyle(document.documentElement);
  ctx.fillStyle = css.getPropertyValue("--muted");
  const start = active()?.window_start ?? 0, end = active()?.window_end ?? p.media.duration;
  const points = (p.waveform || []).filter((point) => point.time >= start && point.time <= end);
  for (const point of points) {
    const x = (point.time - start) / (end - start) * width, bar = Math.max(2, point.energy * 48);
    ctx.fillRect(x, (height - bar) / 2, Math.max(1, width / Math.max(points.length, 1) - 1), bar);
  }
  ctx.fillStyle = css.getPropertyValue("--ink");
  const x = (player.currentTime - start) / (end - start) * width;
  ctx.fillRect(x, 0, 1.5, height);
}
$("search-form").addEventListener("submit", (event) => {
  event.preventDefault(); safely(async () => {
    $("search-button").disabled = true;
    try {
      const results = await job("/api/search", { query: $("search").value }, "Searching YouTube…");
      const grid = $("search-results"); grid.replaceChildren(); grid.hidden = false;
      for (const item of results) {
        const card = node("div", undefined, "search-result"), img = node("img");
        img.src = item.thumbnail; img.alt = ""; img.loading = "lazy";
        const preview = node("button", undefined, "search-preview-button"); preview.type = "button";
        preview.setAttribute("aria-label", `Preview ${item.title}`); preview.dataset.video = item.id;
        preview.append(img, node("strong", item.title), node("span", "Preview video ↗"));
        preview.addEventListener("click", () => safely(() => previewSearchResult(item)));
        const button = node("button", "Import video", "secondary"); button.type = "button";
        button.addEventListener("click", () => safely(async () => {
          button.disabled = true;
          try { await importSource(item.id); } finally { button.disabled = false; }
        }));
        card.append(preview, button); grid.append(card);
      }
      status(`${results.length} videos found. Preview a video before importing.`);
    } finally { $("search-button").disabled = false; }
  });
});
let previewedSearchResult = null, youtubePreviewPlayer = null, youtubeApiPromise = null;
function loadYouTubeApi() {
  if (window.YT?.Player) return Promise.resolve(window.YT);
  if (youtubeApiPromise) return youtubeApiPromise;
  youtubeApiPromise = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("YouTube player could not load.")), 12000);
    window.onYouTubeIframeAPIReady = () => { clearTimeout(timer); resolve(window.YT); };
    const script = document.createElement("script"); script.src = "https://www.youtube.com/iframe_api"; script.async = true;
    script.onerror = () => { clearTimeout(timer); reject(new Error("YouTube player could not load.")); };
    document.head.append(script);
  }).catch((error) => { youtubeApiPromise = null; throw error; });
  return youtubeApiPromise;
}
function previewError(code) {
  $("search-preview-message").textContent = [101, 150].includes(code)
    ? "This video is blocked in embedded players. Watch it on YouTube before importing."
    : "YouTube could not play this embedded preview. Open it on YouTube before importing.";
}
function previewSearchResult(item) {
  if (!/^[A-Za-z0-9_-]{11}$/.test(item.id)) throw new Error("This search result has an invalid video ID.");
  const selected = { id: item.id, title: item.title };
  previewedSearchResult = selected;
  $("search-preview-title").textContent = item.title;
  const imported = state.projects.find((source) => source.id === item.id);
  const localUrl = imported?.media ? sourcePresentation(imported).playbackUrl : null;
  const local = $("search-preview-local"), iframe = $("search-preview-player");
  $("search-preview-stage").hidden = !!localUrl; local.hidden = !localUrl;
  $("search-preview-import").textContent = imported ? "Use this video" : "Import this video";
  $("search-preview-message").textContent = localUrl ? "Previewing the original video already in your library."
    : "If YouTube blocks this preview, open it on YouTube before importing.";
  if (localUrl) {
    iframe.removeAttribute("src"); local.src = localUrl;
  } else {
    const origin = window.location?.origin || "http://127.0.0.1:8476";
    iframe.src = `https://www.youtube-nocookie.com/embed/${item.id}?playsinline=1&rel=0&enablejsapi=1&origin=${encodeURIComponent(origin)}`;
    void loadYouTubeApi().then((YT) => {
      if (previewedSearchResult !== selected) return;
      youtubePreviewPlayer = new YT.Player(iframe, { events: {
        onError: (event) => { if (previewedSearchResult === selected) { selected.embedFailed = true; previewError(event.data); } },
        onReady: () => { if (previewedSearchResult === selected && !selected.embedFailed) $("search-preview-message").textContent = ""; },
      } });
    }).catch(() => { if (previewedSearchResult === selected) previewError(); });
  }
  $("search-preview-external").href = `https://www.youtube.com/watch?v=${item.id}`;
  $("search-preview").showModal();
}
function closeSearchPreview() {
  previewedSearchResult = null;
  const iframe = $("search-preview-player"), local = $("search-preview-local");
  local.pause(); local.removeAttribute("src"); local.load();
  iframe.removeAttribute("src");
  if (youtubePreviewPlayer) {
    const replacement = iframe.cloneNode(false);
    youtubePreviewPlayer.destroy(); youtubePreviewPlayer = null;
    $("search-preview-stage").replaceChildren(replacement);
  }
  $("search-preview").close();
}
$("search-preview-close").addEventListener("click", closeSearchPreview);
$("search-preview").addEventListener("cancel", (event) => { event.preventDefault(); closeSearchPreview(); });
$("search-preview-import").addEventListener("click", () => safely(async () => {
  const item = previewedSearchResult;
  if (!item) return;
  closeSearchPreview();
  if (state.projects.some((source) => source.id === item.id)) {
    projectId = item.id; activeId = null; rememberProject(); render();
    $("frame-heading").scrollIntoView({ block: "start" });
  } else await importSource(item.id);
}));
function playbackCapabilities() {
  try {
    return { av1_mp4: document.createElement("video").canPlayType?.('video/mp4; codecs="av01.0.12M.08"') === "probably" };
  } catch { return { av1_mp4: false }; }
}
async function importSource(id) {
  await job("/api/import", { video_id: id, playback_capabilities: playbackCapabilities() }, "Preparing your video…");
  projectId = id; activeId = null; await refresh(); status("Video ready. Play it and choose a freeze frame.");
}
$("import-seed").addEventListener("click", () => safely(() => importSource(state.seed)));
$("source-select").addEventListener("change", () => {
  projectId = $("source-select").value; rememberProject(); activeId = null; render();
});
$("analyze").addEventListener("click", () => safely(async () => {
  await job("/api/analyze", { video_id: projectId }, "Finding commentator cues and audio peaks…");
  await refresh(); status("Analysis complete. Your previous selections have been kept.");
}));
$("scrub").addEventListener("input", () => seek(Number($("scrub").value)));
$("step-back").addEventListener("click", () => seek(player.currentTime - 1 / project().media.fps));
$("step-forward").addEventListener("click", () => seek(player.currentTime + 1 / project().media.fps));
$("tail").addEventListener("change", () => safely(async () => {
  updateTiming(); if (!active()) return;
  await api("/api/select", { video_id: projectId, item_id: activeId, freeze_time: active().freeze_time ?? active().time, tail_seconds: Number($("tail").value) });
  await refresh();
}));
$("play-cut").addEventListener("click", () => safely(async () => {
  $("freeze-preview").hidden = true; player.currentTime = active().window_start;
  previewEnd = active().window_end; await player.play();
}));
async function saveCurrentFrame() {
  const itemId = activeId, sourceId = projectId;
  $("set-frame").disabled = true; player.pause();
  try {
    const result = await job("/api/frame", { video_id: sourceId, item_id: itemId, time: player.currentTime, tail: state.defaults.tail_seconds }, "Saving this frame…");
    await api("/api/select", { video_id: sourceId, item_id: itemId, selected: true });
    await refresh();
    if (projectId === sourceId && activeId === itemId) {
      $("freeze-preview").src = result.frame_url; $("freeze-preview").hidden = false;
    }
    scheduleActionLabel(state.projects.find((source) => source.id === sourceId),
      state.projects.find((source) => source.id === sourceId)?.candidates?.find((item) => item.id === itemId), result.freeze_time);
    status(`Frame saved at ${clock(result.freeze_time)}. Generate with world models below.`);
  } finally { $("set-frame").disabled = !active(); }
}
$("set-frame").addEventListener("click", () => safely(saveCurrentFrame));
$("freeze-preview").addEventListener("click", () => { $("freeze-preview").hidden = true; });
$("export").addEventListener("click", () => safely(async () => {
  $("export").disabled = true;
  try {
    await job("/api/export", { video_id: projectId, combined: $("combined").checked }, "Preparing selected source clips…");
    await refresh(); status(state.generation.paused
      ? "Source clips are ready below. Generation remains paused."
      : "Source clips are ready below.");
  } finally { renderSelection(); }
}));
$("generation-check").addEventListener("click", () => safely(async () => {
  $("generation-check").disabled = true;
  try {
    await api("/api/generation/check", {});
    await refresh(); status(state.generation.message); resumeGenerations();
  } finally { $("generation-check").disabled = false; }
}));
$("combine-finished").addEventListener("click", () => safely(async () => {
  $("combine-finished").disabled = true;
  try {
    const outputs = (project().generations || []).flatMap((r) => r.composites || []).filter((c) => selectedOutputs.has(c.id)).map((c) => c.id);
    const result = await job("/api/combine", { video_id: projectId, outputs }, "Combining your selected highlights…");
    await refresh(); status("Your combined reel is ready under Outputs.");
    const link = node("a"); link.href = result.url; link.download = `${result.id}.mp4`; link.click();
  } finally { renderOutputs(); }
}));
async function submitGeneration({ referenceRunId = null, freezeTime } = {}) {
  if (pendingGenerations.has(projectId)) { resumeGenerations(); return; }
  const sourceId = projectId, source = project();
  const selected = active() || (source.candidates || []).find((candidate) => candidate.selected);
  const orbitPath = chosenOrbitPath()?.id;
  const record = { video_id: sourceId, started_at: Date.now(), freeze_time: freezeTime ?? selected?.freeze_time,
    known_run_ids: (source.generations || []).map((run) => run.id), ...(orbitPath ? { orbit_path: orbitPath } : {}) };
  pendingGenerations.set(sourceId, record); savePendingGenerations();
  scheduleWorkspaceRefresh();
  $("generate").disabled = true; $("rerun-output").disabled = true;
  try {
    try {
      const started = await api("/api/generate", { video_id: sourceId, mode: "fal-h3-max",
        ...(orbitPath ? { orbit_path: orbitPath } : {}),
        ...(referenceRunId ? { reference_run_id: referenceRunId } : selected?.id ? { item_id: selected.id } : {}) });
      Object.assign(record, { job_id: started.job_id, run_id: started.run_id }); savePendingGenerations();
      try { await refresh(); } catch { /* The saved tracker recovers if the server is restarting. */ }
    } catch (error) {
      if (error.status && error.status >= 400 && error.status < 500) {
        pendingGenerations.delete(sourceId); savePendingGenerations(); throw error;
      }
      status("The submission acknowledgement was interrupted. Checking saved run history before taking any further action.");
    }
    await monitorGeneration(record);
  } finally { renderSelection(); }
}
$("generate").addEventListener("click", () => safely(() => submitGeneration()));
$("rerun-output").addEventListener("click", () => safely(async () => {
  const clip = finishedClips().find((item) => item.id === viewedOutputs.get(projectId));
  if (!clip?.run_id) return;
  await submitGeneration({ referenceRunId: clip.run_id, freezeTime: clip.freeze_time });
}));
player.addEventListener("timeupdate", updatePosition);
player.addEventListener("seeked", updatePosition);
player.addEventListener("error", () => status("Video playback failed. Reimport the source to verify the local media."));
window.addEventListener("resize", drawWave);
document.addEventListener("keydown", (event) => {
  if (!active() || /INPUT|TEXTAREA|SELECT|BUTTON/.test(event.target.tagName)) return;
  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    event.preventDefault(); seek(player.currentTime + (event.key === "ArrowLeft" ? -1 : 1) / project().media.fps);
  }
});
void loadRemixPresets();
safely(async () => {
  if (!projectId && pendingGenerations.size) projectId = [...pendingGenerations.keys()].at(-1);
  try { await refresh(); restoreUpdateView(); } catch (error) {
    if (!pendingGenerations.size) throw error;
    status("Reconnecting to the server and recovering your existing generation.");
  }
  for (const source of state.projects) {
    if (pendingGenerations.has(source.id)) continue;
    const run = (source.generations || []).find((item) => item.provider === "Fal" && ["queued", "running"].includes(item.status));
    if (run) pendingGenerations.set(source.id, { video_id: source.id, run_id: run.id, started_at: Date.now() });
  }
  savePendingGenerations();
  if (pendingGenerations.size) resumeGenerations();
  else status(project()?.status === "preparing_preview" ? sourcePresentation(project()).preparationLabel : project() ? "" : "Search for a video to begin.");
}).finally(() => { scheduleWorkspaceRefresh(); scheduleActivityPoll(0); });
