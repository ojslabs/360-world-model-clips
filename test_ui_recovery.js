"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const storageKey = "football-edits.pending-generations.v1";
function harness(records = []) {
  const storage = new Map([[storageKey, JSON.stringify(records)], ["football-edits.selected-project.v1", JSON.stringify("source")]]);
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      dataset: {}, style: {}, listeners: {}, children: [],
      addEventListener(name, callback) { this.listeners[name] = callback; },
      replaceChildren(...children) { this.children = []; this.append(...children); },
      append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } },
      remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); },
      setAttribute(name, value) { this[name] = value; },
      getAttribute(name) { return this[name] ?? null; },
      removeAttribute(name) { delete this[name]; },
      scrollIntoView() { this.scrolls = (this.scrolls || 0) + 1; },
      pause() { this.paused = true; this.pauses = (this.pauses || 0) + 1; },
      load() { this.currentTime = 0; this.paused = true; this.loads = (this.loads || 0) + 1; },
      play() { this.paused = false; this.plays = (this.plays || 0) + 1; return Promise.resolve(); },
      showModal() { this.open = true; },
      close() { this.open = false; },
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: element, createElement: (tag) => element(`${tag}-${elements.size}`), addEventListener() {} },
    window: { addEventListener() {} },
    localStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) },
    // Initial page loading stays pending; every test injects its own bounded reads.
    fetch: () => new Promise(() => {}), AbortController,
    setTimeout: (...args) => { const timer = setTimeout(...args); timer.unref(); return timer; }, clearTimeout,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "ui/app.js"), "utf8"), context);
  vm.runInContext("globalThis.tracker = { api, redirectToLogin, job, pollGeneration, readGeneration, monitorGeneration, refresh, workspaceRefreshDelay, preferredProject, runProgress, renderOutputRunStatus, newestFinishedOutput, openFinishedOutput, alignInitialOutputs, checkAppBuild, applyAppUpdateWhenIdle, applyWorkspaceReset, updateApp, restoreUpdateView, sourcePresentation, setSourcePlayback, importSource, previewSearchResult, closeSearchPreview, previewError, displayText, updateTiming, renderFramePicker, saveCurrentFrame, finishedClips, renderOutputs, outputLabel, outputResolution, renderOutputViewer, actionTitle, scheduleActionLabel, runNextActionLabel, actionLabelQueues, activityPresentation, activityStateSignature, renderActivity, pollActivity, setActivityCollapsed, isManualSource, records: pendingGenerations, projectId };", context);
  return { context, tracker: context.tracker, storage, element };
}
function options(request, extra = {}) {
  let time = 0;
  const messages = [];
  return { request, now: () => time, wait: async (milliseconds) => { time += milliseconds; },
    timeout: 12000, onStatus: (message) => messages.push(message), messages, ...extra };
}
function missing() { const error = new Error("Unknown job"); error.status = 404; return error; }

test("a lost job switches to durable run reads once and completes", async () => {
  const { tracker } = harness(), calls = [];
  let reads = 0;
  const record = { video_id: "source", job_id: "lost", run_id: "existing" };
  const result = await tracker.pollGeneration(record, options(async (url) => {
    calls.push(url);
    if (url.includes("/jobs/")) throw missing();
    return { status: ++reads === 1 ? "running" : "complete", result: { id: "existing" } };
  }));
  assert.equal(result.status, "complete");
  assert.deepEqual(calls, ["/api/jobs/lost", "/api/runs/source/existing", "/api/runs/source/existing"]);
});

test("a transient connection loss retries reads and reports recovery", async () => {
  const { tracker } = harness();
  let calls = 0;
  const config = options(async () => {
    if (++calls === 1) throw new TypeError("Failed to fetch");
    return { status: "complete" };
  });
  const result = await tracker.pollGeneration({ video_id: "source", run_id: "existing" }, config);
  assert.equal(result.status, "complete");
  assert.equal(calls, 2);
  assert.match(config.messages[0], /Reconnecting/);
});

test("an interrupted durable run returns its local delivery error", async () => {
  const { tracker } = harness();
  const result = await tracker.pollGeneration({ video_id: "source", run_id: "existing" }, options(async () => ({
    status: "interrupted", error: "Local assembly was interrupted. Existing Fal output is preserved.",
  })));
  assert.equal(result.status, "interrupted");
  assert.match(result.error, /Local assembly/);
});

test("bounded polling retains the saved run when the server stays unavailable", async () => {
  const record = { video_id: "source", job_id: "lost", run_id: "existing" };
  const { tracker, storage } = harness([record]);
  let calls = 0;
  const result = await tracker.pollGeneration(tracker.records.get("source"), options(async () => {
    ++calls; throw new TypeError("offline");
  }, { timeout: 6000 }));
  assert.equal(result.status, "unresolved");
  assert.equal(calls, 2);
  assert.equal(JSON.parse(storage.get(storageKey))[0].run_id, "existing");
});

test("reload restores the selected source and pending generation", () => {
  const { tracker } = harness([{ video_id: "source", job_id: "saved", run_id: "existing" }]);
  assert.equal(tracker.projectId, "source");
  assert.equal(tracker.records.get("source").job_id, "saved");
});

test("ambiguous submission recovery waits when more than one run matches", async () => {
  const { tracker } = harness();
  const record = { video_id: "source", freeze_time: 10, known_run_ids: ["old"] };
  const result = await tracker.readGeneration(record, async (url) => {
    assert.equal(url, "/api/state");
    return { projects: [{ id: "source", generations: ["first", "second"].map((id) => ({
      id, provider: "Fal", freeze_time: 10,
    })) }] };
  });
  assert.equal(result.status, "awaiting_acknowledgement");
  assert.equal(record.run_id, undefined);
});

test("lost submission acknowledgement recovers one existing run without a second POST", async () => {
  const { context, storage, element } = harness();
  const calls = [];
  const source = { id: "source", candidates: [{ selected: true, freeze_time: 10 }], generations: [] };
  context.seedSource = source;
  vm.runInContext("state = { projects: [seedSource] }; refresh = async () => {}; renderSelection = () => {};", context);
  context.fetch = async (url, init = {}) => {
    calls.push([init.method || "GET", url]);
    if (init.method === "POST") throw new TypeError("acknowledgement lost");
    const value = url === "/api/state"
      ? { projects: [{ ...source, generations: [{ id: "existing", provider: "Fal", freeze_time: 10 }] }] }
      : { status: "complete", result: { id: "existing" } };
    return { ok: true, status: 200, json: async () => value };
  };
  await element("generate").listeners.click();
  assert.deepEqual(calls, [["POST", "/api/generate"], ["GET", "/api/state"], ["GET", "/api/runs/source/existing"]]);
  assert.deepEqual(JSON.parse(storage.get(storageKey)), []);
});

test("completed recovery clears saved tracking only after outputs refresh", async () => {
  const { context, tracker, storage } = harness([{ video_id: "source", job_id: "lost", run_id: "existing" }]);
  context.fetch = async (url) => url.includes("/jobs/")
    ? { ok: false, status: 404, json: async () => ({ error: "Unknown job" }) }
    : { ok: true, status: 200, json: async () => ({ status: "complete" }) };
  vm.runInContext("refresh = async () => { throw new TypeError('server restarting'); }; renderSelection = () => {};", context);
  await tracker.monitorGeneration(tracker.records.get("source"));
  assert.equal(JSON.parse(storage.get(storageKey))[0].run_id, "existing");
  vm.runInContext("refresh = async () => {};", context);
  await tracker.monitorGeneration(tracker.records.get("source"));
  assert.deepEqual(JSON.parse(storage.get(storageKey)), []);
});

test("an interrupted local task stops polling and never repeats its POST", async () => {
  const { context, tracker } = harness(), calls = [];
  context.fetch = async (url, init = {}) => {
    calls.push([init.method || "GET", url]);
    return { ok: true, status: 200, json: async () => init.method === "POST"
      ? { job_id: "local-task" }
      : { status: "interrupted", error: "Local assembly stopped when the server restarted." } };
  };
  await assert.rejects(tracker.job("/api/assemble", { run_id: "existing" }, "Assembling"), /Local assembly stopped/);
  assert.deepEqual(calls, [["POST", "/api/assemble"], ["GET", "/api/jobs/local-task"]]);
});

test("background refresh preserves unchanged controls and publishes changed outputs", async () => {
  const { context, tracker, element } = harness();
  const snapshot = { projects: [{ id: "source", title: "Source", generations: [] }], jobs: [],
    defaults: { aspect_ratio: "16:9", resolution: "1080P", lead_seconds: 8, orbit_seconds: 6, tail_seconds: 5 },
    generation: { message: "Ready" } };
  context.snapshot = snapshot;
  vm.runInContext("state = JSON.parse(JSON.stringify(snapshot)); globalThis.renders = 0; render = () => { ++globalThis.renders; };", context);
  context.fetch = async () => ({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(snapshot)) });
  await tracker.refresh({ onlyChanged: true });
  assert.equal(context.renders, 0);
  assert.equal(tracker.workspaceRefreshDelay(), 10000);
  snapshot.jobs.push({ status: "running" });
  await tracker.refresh({ onlyChanged: true });
  assert.equal(context.renders, 0);
  assert.equal(tracker.workspaceRefreshDelay(), 3000);
  snapshot.projects[0].generations.push({ id: "completed", composites: [{ id: "new-highlight", url: "/new.mp4" }] });
  await tracker.refresh({ onlyChanged: true });
  assert.equal(context.renders, 1);
  assert.match(element("format-summary").textContent, /1080P generation/);
});

test("a source deep link takes priority over the saved project", () => {
  const { context, tracker } = harness();
  context.URLSearchParams = URLSearchParams;
  context.window.location = { search: "?video=another_source" };
  assert.equal(tracker.preferredProject(), "another_source");
});

test("rerun sends the viewed immutable run once, independent of edited selection", async () => {
  const { context, element } = harness(), submissions = [];
  context.source = { id: "source", candidates: [{ selected: true, freeze_time: 90 }], generations: [{
    id: "immutable-reference", provider: "Fal", status: "complete", url: "/orbit.mp4", freeze_time: 10,
    composites: [{ id: "viewed-highlight", source_window: { freeze_time: 10 } }],
  }] };
  vm.runInContext("state = { projects: [source] }; viewedOutputs.set('source', 'viewed-highlight'); refresh = async () => {}; renderSelection = () => {};", context);
  context.fetch = async (url, init = {}) => {
    if (init.method === "POST") submissions.push(JSON.parse(init.body));
    return { ok: true, status: 200, json: async () => init.method === "POST"
      ? { job_id: "rerun-job", run_id: "rerun-result" } : { status: "complete" } };
  };
  await element("rerun-output").listeners.click();
  assert.deepEqual(submissions, [{ video_id: "source", mode: "fal-h3-max", reference_run_id: "immutable-reference" }]);
});

test("a provider-complete run remains visibly processing during local assembly", () => {
  const { tracker } = harness();
  const progress = tracker.runProgress({ id: "source" }, { id: "new-run", status: "complete", freeze_time: 10 },
    [{ id: "assembly-job", run_id: "new-run", status: "running", stage: "assembling", created_at: 1700000000 }], 1700000045000);
  assert.equal(progress.active, true);
  assert.equal(progress.title, "New run · processing");
  assert.match(progress.message, /Joining source action/);
  assert.equal(progress.elapsed, "45s elapsed");
});

test("failed latest run shows its error and immutable saved frame inside Outputs", () => {
  const { context, tracker, element } = harness();
  vm.runInContext("state = { projects: [{ id: 'source', generations: [{ id: 'failed-run', provider: 'Fal', status: 'failed', freeze_time: 10, error: 'Local audio preparation needs attention.' }] }], jobs: [] };", context);
  tracker.renderOutputRunStatus();
  const container = element("output-run-status"), card = container.children[0];
  assert.equal(container.hidden, false);
  assert.equal(card.dataset.run, "failed-run");
  assert.equal(card.children[0].href, "/media/source/exports/failed-run/frame.png");
  assert.equal(card.children[1].children[0].textContent, "Latest run · needs attention");
  assert.match(card.children[1].children[1].textContent, /Local audio preparation/);
});

test("newest finished output is found across sources using a bound job timestamp", () => {
  const { tracker } = harness();
  const clip = (id) => ({ id, url: `/${id}.mp4`, media: { duration: 19 } });
  const snapshot = { projects: [
    { id: "source", generations: [{ id: "old", status: "complete", created_at: 1700000000, composites: [clip("old-output")] }] },
    { id: "another", generations: [{ id: "new", job_id: "new-job", status: "complete", composites: [clip("new-output")] }] },
  ], jobs: [{ id: "new-job", run_id: "new", created_at: 1700000100 }] };
  const newest = tracker.newestFinishedOutput(snapshot);
  assert.equal(newest.source.id, "another");
  assert.equal(newest.clip.id, "new-output");
  assert.equal(newest.created, 1700000100000);
});

test("opening newest output switches source and pins its matching output", () => {
  const { context, tracker, element } = harness();
  context.targetOutput = { source: { id: "another" }, clip: { id: "new-output" } };
  vm.runInContext("render = () => {}; renderOutputViewer = () => {};", context);
  tracker.openFinishedOutput(context.targetOutput);
  assert.equal(vm.runInContext("projectId", context), "another");
  assert.equal(vm.runInContext("viewedOutputs.get(projectId)", context), "new-output");
  assert.equal(element("source-select").value, "another");
  assert.equal(element("outputs").scrolls, 1);
});

test("an Outputs deep link aligns once after asynchronous content is ready", async () => {
  const { context, tracker, element } = harness();
  context.window.location = { hash: "#outputs" };
  context.window.requestAnimationFrame = (callback) => callback();
  vm.runInContext("state = { projects: [{ id: 'source' }] };", context);
  await tracker.alignInitialOutputs();
  await tracker.alignInitialOutputs();
  assert.equal(element("outputs").scrolls, 1);
});

test("a changed build shows an update notice without navigating and throttles reads", async () => {
  const { tracker, element } = harness();
  const current = "a".repeat(64), next = "b".repeat(64), calls = [];
  const request = async (url, init) => {
    calls.push([url, init.method, init.cache]);
    return { ok: true, text: async () => `<meta name="football-edits-build" content="${next}">` };
  };
  await tracker.checkAppBuild({ request, currentVersion: current, now: 100000 });
  await tracker.checkAppBuild({ request, currentVersion: current, now: 100500 });
  assert.equal(element("app-update-notice").hidden, false);
  assert.deepEqual(calls, [["/?ui-build-check=100000", undefined, "no-store"]]);
});

test("an identical build does not offer an update", async () => {
  const { tracker, element } = harness(), current = "a".repeat(64);
  await tracker.checkAppBuild({ currentVersion: current, now: 100000,
    request: async () => ({ ok: true, text: async () => `<meta name="football-edits-build" content="${current}">` }) });
  assert.equal(element("app-update-notice").hidden, true);
});

test("idle automatic update saves playback positions and navigates only once", async () => {
  const { context, tracker, element } = harness(), session = new Map(), destinations = [];
  context.sessionStorage = { setItem: (key, value) => session.set(key, value), getItem: (key) => session.get(key), removeItem: (key) => session.delete(key) };
  context.URL = URL;
  context.window.location = { href: "http://127.0.0.1:8476/?video=source#outputs", replace: (url) => destinations.push(url) };
  Object.assign(element("player"), { currentTime: 52, paused: true });
  Object.assign(element("output-player"), { currentTime: 6.5, paused: true });
  element("freeze-preview").hidden = true;
  vm.runInContext("viewedOutputs.set('source', 'saved-output');", context);
  const next = "b".repeat(64);
  await tracker.checkAppBuild({ currentVersion: "a".repeat(64), now: 100000,
    request: async () => ({ ok: true, text: async () => `<meta name="football-edits-build" content="${next}">` }) });
  assert.equal(destinations.length, 1);
  tracker.applyAppUpdateWhenIdle(); tracker.updateApp();
  assert.equal(destinations.length, 1);
  assert.equal(new URL(destinations[0]).searchParams.get("ui"), next);
  const saved = JSON.parse(session.get("football-edits.update-view.v1"));
  assert.equal(saved.output_id, "saved-output");
  assert.equal(saved.output_time, 6.5);
  assert.equal(saved.source_time, 52);
  assert.equal(new URL(destinations[0]).hash, "#outputs");
});

test("highest-quality source and full-resolution compatible preview are labelled separately", () => {
  const { tracker } = harness();
  const source = { id: "source", media: { width: 3840, height: 2160, fps: 59.94 },
    original_url: "/source.mkv", preview_url: "/preview.mp4", video_url: "/old.mp4",
    preview: { kind: "transcode", media: { width: 3840, height: 2160, fps: 59.94 }, resolution_preserved: true },
    source_quality: { selection: "best_available" } };
  const view = tracker.sourcePresentation(source);
  assert.equal(view.originalLabel, "Highest available original · 3840 × 2160");
  assert.equal(view.previewLabel, "Browser preview · 3840 × 2160");
  assert.equal(view.originalUrl, "/source.mkv");
  assert.equal(view.playbackUrl, "/preview.mp4");
  assert.match(view.note, /Freeze frames are extracted from the original/);
  assert.equal(source.media.fps, 59.94);
});

test("legacy imports do not claim a verified highest-quality download", () => {
  const { tracker } = harness();
  const view = tracker.sourcePresentation({ id: "source", media: { width: 1920, height: 1080 },
    video_url: "/source.mp4", source_quality: { selection: "existing_unverified" } });
  assert.equal(view.originalLabel, "Imported original · 1920 × 1080");
  assert.equal(view.previewLabel, "");
  assert.equal(view.originalUrl, "/source.mp4");
  assert.equal(view.playbackUrl, "/source.mp4");
});

test("a saved 4K original stays visible while its browser preview is being prepared", () => {
  const { context, tracker } = harness();
  const source = { id: "new-source", status: "preparing_preview", media: { width: 3840, height: 2160, fps: 29.97 },
    original_url: "/media/new-source/source.mkv", preview_url: null, video_url: null,
    source_quality: { selection: "best_available" } };
  const view = tracker.sourcePresentation(source);
  assert.equal(view.preparationLabel, "Preparing 4K playback");
  assert.equal(view.previewLabel, "Preparing 4K playback");
  assert.equal(view.playbackUrl, null);
  assert.equal(view.originalUrl, "/media/new-source/source.mkv");
  assert.match(view.note, /original is saved/);
  context.preparingSource = source;
  vm.runInContext("state = { projects: [preparingSource], jobs: [] };", context);
  assert.equal(tracker.workspaceRefreshDelay(), 3000);
});

test("a preparing source clears stale playback and loads only the actual preview when ready", () => {
  const { tracker, element } = harness(), player = element("player");
  Object.assign(player, { src: "/old-source.mp4", currentTime: 120, paused: false });
  player.dataset.source = "old-source";
  const source = { id: "new-source", status: "preparing_preview", media: { width: 3840, height: 2160 },
    original_url: "/media/new-source/source.mkv", preview_url: null, video_url: null };
  tracker.setSourcePlayback(source);
  assert.equal(player.getAttribute("src"), null);
  assert.equal(player.paused, true);
  assert.equal(player.controls, false);
  assert.equal(player.loads, 1);
  assert.equal(element("source-playback-status").hidden, false);
  assert.match(element("source-playback-status").textContent, /Preparing 4K playback/);
  assert.equal(element("freeze-preview").hidden, true);
  tracker.setSourcePlayback(source);
  assert.equal(player.loads, 1);
  source.status = "ready";
  source.preview_url = "/media/new-source/source-preview.mp4";
  tracker.setSourcePlayback(source);
  assert.equal(player.getAttribute("src"), source.preview_url);
  assert.equal(player.controls, true);
  assert.equal(element("source-playback-status").hidden, true);
});

test("an import forwards AV1 support only when the browser reports probably", async () => {
  const { context, tracker } = harness(), requests = [], probes = [];
  context.document.createElement = () => ({ canPlayType: (type) => { probes.push(type); return "probably"; } });
  context.captureImport = async (url, payload) => { requests.push({ url, payload }); };
  vm.runInContext("job = captureImport; refresh = async () => {};", context);
  await tracker.importSource("new-source");
  assert.deepEqual(probes, ['video/mp4; codecs="av01.0.12M.08"']);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/api/import");
  assert.equal(requests[0].payload.video_id, "new-source");
  assert.equal(requests[0].payload.playback_capabilities.av1_mp4, true);
  assert.equal(vm.runInContext("projectId", context), "new-source");
});

test("an import conservatively disables AV1 for maybe, absent, or failed browser probes", async () => {
  for (const createVideo of [() => ({ canPlayType: () => "maybe" }), () => ({ canPlayType: () => "" }),
    () => ({}), () => ({ canPlayType() { throw new Error("Unsupported probe"); } })]) {
    const { context, tracker } = harness(), requests = [];
    context.document.createElement = createVideo;
    context.captureImport = async (url, payload) => { requests.push({ url, payload }); };
    vm.runInContext("job = captureImport; refresh = async () => {};", context);
    await tracker.importSource("new-source");
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, "/api/import");
    assert.equal(requests[0].payload.video_id, "new-source");
    assert.equal(requests[0].payload.playback_capabilities.av1_mp4, false);
  }
});

test("a compatible-preview switch keeps the same source position and playback", () => {
  const { tracker, element } = harness(), player = element("player");
  Object.assign(player, { src: "/old.mp4", currentTime: 120, duration: 600, paused: false });
  player.dataset.source = "source";
  const source = { id: "source", media: { width: 3840, height: 2160 },
    video_url: "/source.mkv", preview_url: "/preview.mp4" };
  tracker.setSourcePlayback(source);
  assert.equal(player.src, "/preview.mp4");
  player.currentTime = 0; player.listeners.loadedmetadata();
  assert.equal(player.currentTime, 120);
  assert.equal(player.paused, false);
  const pauses = player.pauses;
  tracker.setSourcePlayback(source);
  assert.equal(player.pauses, pauses);
});

test("a direct-cut revision keeps its Clip number and exposes its previous edit", () => {
  const { context, tracker, element } = harness();
  vm.runInContext("state = { projects: [{ id: 'source', media: { width: 1920, height: 1080 }, video_url: '/source.mp4', generations: [] }], generation: { ready: true }, defaults: { lead_seconds: 8, orbit_seconds: 6, tail_seconds: 5 } }; viewedOutputs.set('source', 'revised-output');", context);
  const clip = { id: "revised-output", run_id: "run-4", test_number: 4, freeze_time: 120,
    media: { duration: 19 }, url: "/direct-cut.mp4", tail_seconds: 5,
    transition: "cut", join_version: "cut_next_frame_v1", previous_edit_url: "/previous.mp4",
    reference_closure: { first_last_pixel_hash_equal: true } };
  tracker.renderOutputViewer([clip]);
  assert.match(tracker.outputLabel(clip), /^Clip 4 .*direct cut/);
  assert.equal(element("output-title").textContent, "Clip 4 · 19s highlight · direct cut");
  assert.equal(element("output-transition").textContent, "Direct cut; action resumes on the next source frame.");
  assert.match(element("output-return").textContent, /edited clip/);
  assert.equal(element("output-previous").href, "/previous.mp4");
  assert.equal(element("output-previous").hidden, false);
  clip.audio_provenance = { orbit: "source_slow_motion_loop" };
  delete clip.transition; delete clip.previous_edit_url;
  tracker.renderOutputViewer([clip]);
  assert.equal(element("output-previous").hidden, true);
  assert.equal(element("output-transition").hidden, true);
  assert.match(element("output-audio").textContent, /Source audio with a slow-motion effect/);
  assert.match(element("output-audio").textContent, /return to normal speed/);
});

test("search preview opens the actual embed without import or generation and clears playback on close", async () => {
  const { context, tracker, element } = harness(), calls = [];
  context.fetch = async (url) => { calls.push(url); throw new Error("Preview must not call the local API"); };
  tracker.previewSearchResult({ id: "Wg_jwgmwt-U", title: "Skateboarding" });
  assert.equal(element("search-preview").open, true);
  assert.equal(element("search-preview-title").textContent, "Skateboarding");
  const embed = new URL(element("search-preview-player").src);
  assert.equal(embed.origin, "https://www.youtube-nocookie.com");
  assert.equal(embed.pathname, "/embed/Wg_jwgmwt-U");
  assert.equal(embed.searchParams.get("enablejsapi"), "1");
  assert.equal(embed.searchParams.get("origin"), "http://127.0.0.1:8476");
  assert.equal(element("search-preview-external").href, "https://www.youtube.com/watch?v=Wg_jwgmwt-U");
  element("search-preview-close").listeners.click();
  assert.equal(element("search-preview").open, false);
  assert.equal(element("search-preview-player").getAttribute("src"), null);
  tracker.previewSearchResult({ id: "Wg_jwgmwt-U", title: "Skateboarding" });
  let cancelled = false;
  element("search-preview").listeners.cancel({ preventDefault() { cancelled = true; } });
  assert.equal(cancelled, true);
  assert.equal(element("search-preview-player").getAttribute("src"), null);
  assert.deepEqual(calls, []);
});

test("an imported search result previews its existing video without a remote embed or reimport", async () => {
  const { context, tracker, element } = harness(), calls = [];
  context.source = { id: "Wg_jwgmwt-U", title: "Skateboarding", status: "ready",
    media: { width: 3840, height: 2160 }, preview_url: "/media/Wg_jwgmwt-U/source-preview.mp4" };
  vm.runInContext("state = { projects: [source] }; render = () => {};", context);
  context.fetch = async (url) => { calls.push(url); throw new Error("No import expected"); };
  tracker.previewSearchResult(context.source);
  assert.equal(element("search-preview-local").src, context.source.preview_url);
  assert.equal(element("search-preview-stage").hidden, true);
  assert.equal(element("search-preview-local").hidden, false);
  assert.equal(element("search-preview-player").getAttribute("src"), null);
  assert.equal(element("search-preview-import").textContent, "Use this video");
  await element("search-preview-import").listeners.click();
  assert.equal(vm.runInContext("projectId", context), "Wg_jwgmwt-U");
  assert.equal(element("search-preview-local").getAttribute("src"), null);
  assert.equal(element("search-preview-local").paused, true);
  assert.deepEqual(calls, []);
});

test("blocked remote previews show a watch-before-import fallback", () => {
  const { tracker, element } = harness();
  tracker.previewError(150);
  assert.match(element("search-preview-message").textContent, /blocked in embedded players/);
  assert.match(element("search-preview-message").textContent, /before importing/);
});

test("presentation hides the provider name while preserving raw receipts and derived timing", () => {
  const { context, tracker, element } = harness();
  const receipt = { provider: "Fal", error: "Fal rejected the request. FAL_KEY remains server-only." };
  const shown = tracker.displayText(receipt.error);
  assert.doesNotMatch(shown, /\bFal\b|FAL_KEY/i);
  assert.equal(receipt.provider, "Fal");
  assert.match(receipt.error, /Fal/);
  const historicalJob = { kind: "Generate H3 Max camera orbit" };
  assert.equal(tracker.displayText(historicalJob.kind), "Generate with world models");
  assert.equal(historicalJob.kind, "Generate H3 Max camera orbit");
  vm.runInContext("state = { projects: [], defaults: { lead_seconds: 3, orbit_seconds: 6, tail_seconds: 4 } };", context);
  tracker.updateTiming();
  assert.equal(element("timing-summary").textContent, "3s action + 6s generated moment + up to 4s resumed action = up to 13s");
});

test("the compact picker restores a saved frame without changing batch selections", () => {
  const { context, tracker, element } = harness();
  context.source = { id: "source", media: { duration: 600 }, candidates: [
    { id: "first", time: 30, label: "First", selected: true },
    { id: "saved", time: 50, freeze_time: 51.5, frame_url: "/saved.png", label: "Saved", selected: true },
  ] };
  vm.runInContext("state = { projects: [source] }; activeId = null;", context);
  Object.assign(element("player"), { readyState: 1, currentTime: 0 });
  tracker.renderFramePicker(context.source, false);
  assert.equal(element("moment-select").value, "saved");
  assert.equal(element("player").currentTime, 51.5);
  assert.equal(context.source.candidates.filter((candidate) => candidate.selected).length, 2);
  assert.equal(element("moment-select").disabled, false);
});

test("Use this frame saves the playhead and keeps the other batch selections", async () => {
  const { context, tracker, element } = harness(), jobs = [], selects = [];
  context.source = { id: "source", candidates: [
    { id: "kept", selected: true }, { id: "current", selected: false },
  ] };
  context.saveJob = async (url, data) => { jobs.push({ url, data }); return { frame_url: "/frame.png", freeze_time: 123 }; };
  vm.runInContext("state = { projects: [source], defaults: { tail_seconds: 5 } }; activeId = 'current'; job = saveJob; refresh = async () => {};", context);
  context.fetch = async (url, init) => { selects.push({ url, data: JSON.parse(init.body) }); return { ok: true, json: async () => ({}) }; };
  element("player").currentTime = 123;
  await tracker.saveCurrentFrame();
  assert.equal(jobs.length, 1);
  assert.equal(jobs[0].url, "/api/frame");
  assert.equal(jobs[0].data.time, 123);
  assert.equal(jobs[0].data.item_id, "current");
  assert.deepEqual(selects, [{ url: "/api/select", data: { video_id: "source", item_id: "current", selected: true } }]);
  assert.equal(context.source.candidates[0].selected, true);
  assert.equal(element("freeze-preview").src, "/frame.png");
});

test("Generate sends the active saved frame independently of multiple batch selections", async () => {
  const { context, element } = harness(), submissions = [];
  context.source = { id: "source", candidates: [
    { id: "batch", selected: true, freeze_time: 30 }, { id: "current", selected: true, freeze_time: 123, frame_url: "/frame.png" },
  ], generations: [] };
  vm.runInContext("state = { projects: [source] }; activeId = 'current'; refresh = async () => {}; renderSelection = () => {};", context);
  context.fetch = async (url, init = {}) => {
    if (init.method === "POST") submissions.push(JSON.parse(init.body));
    return { ok: true, status: 200, json: async () => init.method === "POST"
      ? { job_id: "new-job", run_id: "new-run" } : { status: "complete" } };
  };
  await element("generate").listeners.click();
  assert.deepEqual(submissions, [{ video_id: "source", mode: "fal-h3-max", item_id: "current" }]);
});

test("three-step markup retains every wired control and collapses inspection by default", () => {
  const html = fs.readFileSync(path.join(__dirname, "ui/sections/00_editor.html"), "utf8");
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(new Set(ids).size, ids.length);
  const script = fs.readFileSync(path.join(__dirname, "ui/app.js"), "utf8");
  for (const match of script.matchAll(/\$\("([^"]+)"\)/g)) assert.ok(ids.includes(match[1]), match[1]);
  assert.equal((html.match(/<section\b/g) || []).length, 3);
  assert.doesNotMatch(html, /<details[^>]*\bopen\b/);
  assert.doesNotMatch(html, /768[pP]/);
  assert.match(html, /360° World Model Clips/);
  assert.match(html, /id="fal-account"/);
  assert.match(html, /Fal API key/);
});

test("reload selects the newest revision of a run while preserving older edits", () => {
  const { context, tracker, element } = harness();
  vm.runInContext("state = { projects: [{ id: 'source', title: 'Source', media: { width: 1920, height: 1080 }, video_url: '/source.mp4', generations: [{ id: 'run', provider: 'Fal', status: 'complete', freeze_time: 40, url: '/orbit.mp4', composites: [{ id: 'old-19', url: '/old-19.mp4', media: { duration: 19 }, tail_seconds: 5 }, { id: 'new-13', url: '/new-13.mp4', media: { duration: 13 }, tail_seconds: 4 }] }] }], defaults: { lead_seconds: 3, orbit_seconds: 6, tail_seconds: 4 }, generation: { ready: true }, jobs: [] };", context);
  tracker.renderOutputs();
  assert.equal(element("output-picker").value, "new-13");
  assert.equal(element("output-player").src, "/new-13.mp4");
  assert.equal(tracker.finishedClips()[0].media.duration, 13);
  assert.equal(vm.runInContext("state.projects[0].generations[0].composites[0].id", context), "old-19");
});

test("a newly assembled revision is selected once and ordinary refresh preserves review choice", () => {
  const { context, tracker, element } = harness();
  vm.runInContext("state = { projects: [{ id: 'source', title: 'Source', media: { width: 1920, height: 1080 }, video_url: '/source.mp4', generations: [{ id: 'run', provider: 'Fal', status: 'complete', freeze_time: 40, url: '/orbit.mp4', composites: [{ id: 'old-19', url: '/old-19.mp4', media: { duration: 19 }, tail_seconds: 5 }] }] }], defaults: { lead_seconds: 3, orbit_seconds: 6, tail_seconds: 4 }, generation: { ready: true }, jobs: [] };", context);
  tracker.renderOutputs();
  assert.equal(element("output-picker").value, "old-19");
  vm.runInContext("state.projects[0].generations[0].composites.push({ id: 'new-13', url: '/new-13.mp4', media: { duration: 13 }, tail_seconds: 4 });", context);
  tracker.renderOutputs();
  assert.equal(element("output-picker").value, "new-13");
  vm.runInContext("viewedOutputs.set('source', 'old-19');", context);
  tracker.renderOutputs();
  element("output-player").currentTime = 7;
  const pauses = element("output-player").pauses;
  tracker.renderOutputs();
  assert.equal(element("output-picker").value, "old-19");
  assert.equal(element("output-player").currentTime, 7);
  assert.equal(element("output-player").pauses, pauses);
});

test("activity reports measured stream progress and keeps unknown progress indeterminate", () => {
  const { tracker } = harness();
  const job = { id: "import", status: "running", kind: "Import video", created_at: 1000,
    progress: { stage: "downloading", scope: "current_stream", stream_id: "401", percent: 42.5,
      downloaded_bytes: 425000000, total_bytes: 1000000000, speed_bytes_per_second: 10000000, eta_seconds: 58 } };
  const measured = tracker.activityPresentation(job, 1030000);
  assert.equal(measured.label, "Downloading stream 401");
  assert.equal(measured.percent, 42.5);
  assert.equal(measured.elapsed, "30s elapsed");
  assert.match(measured.metrics, /425.0 MB \/ 1.0 GB/);
  assert.match(measured.metrics, /10.0 MB\/s/);
  assert.match(measured.metrics, /58s remaining in this stream/);
  const unknown = tracker.activityPresentation({ ...job, progress: { stage: "generating", percent: null } }, 1030000);
  assert.equal(unknown.percent, null);
  assert.equal(unknown.metrics, "");
  assert.equal(tracker.activityPresentation({ ...job, progress: { percent: 150 } }).percent, null);
});

test("activity progress patches one card without a project render and pops open at completion", async () => {
  const { context, tracker, element } = harness();
  let refreshes = 0;
  const job = { id: "job", status: "running", kind: "Import video", created_at: Date.now() / 1000,
    progress: { stage: "downloading", percent: 10 } };
  context.readActivity = async () => ({ jobs: [job] });
  context.countRefresh = async () => { refreshes++; };
  vm.runInContext("api = readActivity; refresh = countRefresh;", context);
  const video = element("player"); video.src = "/existing.mp4"; video.currentTime = 45; video.paused = false;
  await tracker.pollActivity();
  const card = element("activity-jobs").children[0];
  assert.equal(element("activity-toggle")["aria-expanded"], "true");
  tracker.setActivityCollapsed(true);
  job.progress.percent = 20;
  await tracker.pollActivity();
  assert.equal(refreshes, 1);
  assert.equal(element("activity-jobs").children[0], card);
  assert.equal(element("activity-toggle")["aria-expanded"], "false");
  job.progress = { stage: "preparing_preview" };
  await tracker.pollActivity();
  assert.equal(refreshes, 2);
  assert.equal(card.children[3]["aria-valuenow"], undefined);
  assert.equal(card.children[3].dataset.indeterminate, "true");
  job.status = "complete"; job.updated_at = Date.now() / 1000;
  await tracker.pollActivity();
  assert.equal(refreshes, 3);
  assert.equal(element("activity-toggle")["aria-expanded"], "true");
  assert.match(element("activity-announcement").textContent, /Ready/);
  assert.equal(video.src, "/existing.mp4"); assert.equal(video.currentTime, 45); assert.equal(video.paused, false);
});

test("action labels need confident evidence at the same source frame", () => {
  const { tracker } = harness();
  const label = { status: "complete", label: "Skateboarder jumping onto ledge", confidence: "high", source_time: 10 };
  assert.equal(tracker.actionTitle(label, 10, 30), label.label);
  assert.equal(tracker.actionTitle({ ...label, confidence: "low" }, 10, 30), null);
  assert.equal(tracker.actionTitle(label, 11, 30), null);
  assert.equal(tracker.actionTitle({ ...label, status: "uncertain" }, 10, 30), null);
});

function labelHarness() {
  const h = harness(), timers = new Map(); let timerId = 0;
  h.context.setTimeout = (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; };
  h.context.clearTimeout = (id) => timers.delete(id);
  h.context.labelSource = { id: "source", media: { fps: 30 }, candidates: [
    { id: "a", time: 10, freeze_time: 10, label: "Audio peak" },
    { id: "b", time: 20, freeze_time: 20, label: "Caption cue" },
  ] };
  vm.runInContext("state = { projects: [labelSource], action_labeling: { ready: true } }; activeId = 'a'; render = () => {}; refresh = async () => {};", h.context);
  return { ...h, source: h.context.labelSource, timers };
}

test("label debounce keeps only the newest moment and never overlaps requests", async () => {
  const { context, tracker, source, timers } = labelHarness();
  const calls = []; let finish;
  context.labelRequest = (record) => { calls.push(record); return new Promise((resolve) => { finish = resolve; }); };
  vm.runInContext("requestActionLabel = labelRequest;", context);
  tracker.scheduleActionLabel(source, source.candidates[0]);
  tracker.scheduleActionLabel(source, source.candidates[1]);
  assert.equal(timers.size, 1); assert.equal([...timers.values()][0].delay, 400);
  const first = tracker.runNextActionLabel("source");
  assert.equal(calls.length, 1); assert.equal(calls[0].item_id, "b");
  source.candidates[0].freeze_time = 11;
  tracker.scheduleActionLabel(source, source.candidates[0]);
  source.candidates[0].freeze_time = 12;
  tracker.scheduleActionLabel(source, source.candidates[0]);
  await tracker.runNextActionLabel("source");
  assert.equal(calls.length, 1);
  finish({ video_id: "source", item_id: "b", labelresult: { source_time: 20, status: "complete", confidence: "high", label: "Landing" } });
  await first;
  const second = tracker.runNextActionLabel("source");
  assert.equal(calls.length, 2); assert.equal(calls[1].time, 12);
  source.candidates[0].freeze_time = 13;
  finish({ video_id: "source", item_id: "a", labelresult: { source_time: 12, status: "complete", confidence: "high", label: "Old action" } });
  await second;
  assert.equal(source.candidates[0].action_label, undefined);
  assert.equal(source.candidates[1].action_label.label, "Landing");
});

test("a timed out label is cached locally and a busy response can defer safely", async () => {
  const { context, tracker, source } = labelHarness();
  let calls = 0;
  context.labelRequest = async () => { calls++; const error = new Error("busy"); error.status = calls === 1 ? 409 : 504; throw error; };
  vm.runInContext("requestActionLabel = labelRequest;", context);
  tracker.scheduleActionLabel(source, source.candidates[0]);
  await tracker.runNextActionLabel("source");
  assert.equal(source.candidates[0].action_label, undefined);
  assert.ok(tracker.actionLabelQueues.get("source").next);
  await tracker.runNextActionLabel("source");
  assert.equal(source.candidates[0].action_label.status, "uncertain");
  tracker.scheduleActionLabel(source, source.candidates[0]);
  await tracker.runNextActionLabel("source");
  assert.equal(calls, 2);
});

test("manual mode defaults to the full source while preserving a saved user frame", () => {
  const { context, tracker, element } = harness();
  const source = { id: "source", selection_mode: "manual", media: { fps: 30 }, candidates: [
    { id: "old", selected: true, label: "Audio peak", time: 20 },
    { id: "manual", kind: "manual", label: "Full video", time: 8 },
  ] };
  context.manualSource = source;
  vm.runInContext("state = { projects: [manualSource] };", context);
  tracker.renderFramePicker(source, false);
  assert.equal(element("moment-select").value, "manual");
  source.candidates[0].frame_url = "/saved.png";
  vm.runInContext("activeId = null;", context);
  tracker.renderFramePicker(source, false);
  assert.equal(element("moment-select").value, "old");
  assert.equal(source.candidates[0].selected, true);
  assert.equal(tracker.isManualSource(source), true);
  vm.runInContext("renderSelection = () => {}; drawWave = () => {}; renderActive = () => {}; setSourcePlayback = () => {}; render();", context);
  assert.equal(element("cutups-details").hidden, true);
  assert.equal(element("moment-picker").hidden, true);
  const html = fs.readFileSync(path.join(__dirname, "ui/sections/00_editor.html"), "utf8");
  assert.match(html, /<details id="cutups-details"[^>]*>(?:(?!<\/details>)[\s\S])*id="analyze"/);
});

test("4K exports label the actual 1080p generation and its upscale separately", () => {
  const { tracker } = harness();
  const source = { media: { width: 3840, height: 2160 } };
  const clip = { media: { width: 3840, height: 2160 }, native_generation_media: { width: 1920, height: 1080 },
    resolution_provenance: { generated_size: [1920, 1080], generated_orbit_upscaled: true } };
  assert.equal(tracker.outputResolution(clip, source), "Source 4K · Generated 1080p · Export 4K (generated section upscaled)");
  assert.equal(tracker.outputResolution({ media: clip.media }, source), "Source 4K · Export 4K");
  assert.equal(tracker.outputResolution({ ...clip, resolution_provenance: {} }, source), "Source 4K · Generated 1080p · Export 4K");
});

test("automatic app updates wait for playback, editing, previews and jobs to finish", async () => {
  for (const busy of ["playback", "input", "preview", "job", "pending", "local"]) {
    const { context, tracker, element } = harness(), destinations = [];
    context.URL = URL;
    context.window.location = { href: "http://127.0.0.1:8476/?video=source", replace: (url) => destinations.push(url) };
    Object.assign(element("player"), { paused: true, currentTime: 10 });
    Object.assign(element("output-player"), { paused: true, currentTime: 2 });
    if (busy === "playback") element("output-player").paused = false;
    if (busy === "input") context.document.activeElement = { tagName: "INPUT" };
    if (busy === "preview") element("search-preview").open = true;
    if (busy === "job") vm.runInContext("state.jobs = [{ id: 'existing', status: 'running' }];", context);
    if (busy === "pending") tracker.records.set("source", { run_id: "existing" });
    if (busy === "local") vm.runInContext("activeLocalJobs = 1;", context);
    await tracker.checkAppBuild({ currentVersion: "a".repeat(64), now: 100000,
      request: async () => ({ ok: true, text: async () => `<meta name="football-edits-build" content="${"b".repeat(64)}">` }) });
    assert.equal(destinations.length, 0, busy);
    assert.equal(element("app-update-notice").hidden, false, busy);
    element("output-player").paused = true; element("search-preview").open = false;
    context.document.activeElement = null;
    tracker.records.clear(); vm.runInContext("state.jobs = []; activeLocalJobs = 0;", context);
    context.readActivity = async () => ({ jobs: [] });
    vm.runInContext("api = readActivity; refresh = async () => {};", context);
    await tracker.pollActivity();
    assert.equal(destinations.length, 1, busy);
  }
});

test("demo copy uses world models without exposing provider or request terminology", () => {
  const { tracker } = harness();
  for (const legacy of ["Generate H3 Max camera orbit", "FAL H3 MAX", "FastH3 camera test", "foul camera controls", "Test 3", "https://fal.ai/models/minimax/h3-max/camera-controls"]) {
    assert.doesNotMatch(tracker.displayText(legacy), /h3|\bfal\b|\bfoul\b|\bcamera\b|\btest\b/i);
  }
  const html = fs.readFileSync(path.join(__dirname, "ui/sections/00_editor.html"), "utf8");
  assert.match(html, />Generate with world models</);
  assert.match(html, /placeholder="Find a video"/);
  assert.match(html, /class="compact-details connection-details" hidden/);
  assert.match(html, /class="compact-details source-exports" hidden/);
  assert.doesNotMatch(html.replace(/      <div id="fal-account"[\s\S]*?(?=      <div class="section-row"><h1)/, "").replace(/<[^>]+>/g, ""), /\bH3\b|\bfal\b|\bfoul\b|\bcamera\b|\btests?\b/i);
});

test("workspace reset clears only app tracking, old players and the source deep link once", () => {
  const { context, tracker, storage, element } = harness([{ video_id: "source", job_id: "old", run_id: "old" }]);
  const session = new Map([["football-edits.update-view.v1", "old-view"], ["unrelated", "keep"]]), navigations = [];
  storage.set("unrelated", "keep");
  context.URL = URL;
  context.sessionStorage = { removeItem: (key) => session.delete(key) };
  context.window.location = { href: "http://127.0.0.1:8476/?video=source&ui=old#outputs" };
  context.window.history = { replaceState: (_, __, url) => navigations.push(String(url)) };
  element("player").src = "/old-source.mp4"; element("output-player").src = "/old-output.mp4";
  element("search").value = "old football query";
  assert.equal(tracker.applyWorkspaceReset({ workspace_reset: "fresh-demo" }), true);
  assert.equal(tracker.records.size, 0);
  assert.equal(element("player").src, undefined); assert.equal(element("output-player").src, undefined);
  assert.equal(element("output-workbench").hidden, true); assert.equal(element("search").value, "");
  assert.equal(storage.get("unrelated"), "keep"); assert.equal(session.get("unrelated"), "keep");
  assert.equal(session.has("football-edits.update-view.v1"), false);
  assert.equal(new URL(navigations[0]).searchParams.has("video"), false);
  assert.equal(new URL(navigations[0]).hash, "");
  assert.equal(tracker.applyWorkspaceReset({ workspace_reset: "fresh-demo" }), false);
  assert.equal(navigations.length, 1);
});

test("a reset cancels an already waiting generation monitor without an old-job error", async () => {
  const { tracker } = harness(); let live = true, reads = 0;
  const result = await tracker.pollGeneration({ job_id: "old" }, options(async () => {
    reads++; live = false; throw missing();
  }, { isCurrent: () => live }));
  assert.equal(result.status, "cancelled"); assert.equal(reads, 1);
});

test("public UI uses system fonts without bundled font binaries or private branding", () => {
  const css = fs.readFileSync(path.join(__dirname, "ui/extra.css"), "utf8");
  const html = fs.readFileSync(path.join(__dirname, "ui/sections/00_editor.html"), "utf8");
  assert.match(css, /--body:-apple-system,BlinkMacSystemFont/);
  assert.match(css, /--mono:ui-monospace,SFMono-Regular/);
  assert.doesNotMatch(css, /@font-face|Aeonik|Suisse|Pixelify|\/assets\/fonts\//i);
  assert.match(html, /Workspace · OJS Labs/); assert.doesNotMatch(html, /Cortex/);
  const files = fs.readdirSync(path.join(__dirname, "ui"), { recursive: true });
  assert.equal(files.some((file) => /\.(woff2?|ttf|otf)$/i.test(file)), false);
});

test("an expired demo session redirects once and preserves tracking without retrying a POST", async () => {
  const record = { video_id: "source", job_id: "saved", run_id: "existing" };
  const { context, tracker, storage } = harness([record]), calls = [], destinations = [];
  context.URL = URL;
  context.window.location = { href: "https://demo.example/?video=source#outputs", replace: (url) => destinations.push(url) };
  context.fetch = async (url, init) => {
    calls.push([url, init.method || "GET"]);
    return { ok: false, status: 401, json: async () => ({ error: "Sign in to this shared demo.", login_url: "/login" }) };
  };
  await assert.rejects(tracker.api("/api/generate", { video_id: "source" }), (error) => error.status === 401);
  await assert.rejects(tracker.api("/api/activity"), (error) => error.status === 401);
  assert.equal(destinations.length, 1);
  const login = new URL(destinations[0], "https://demo.example");
  assert.equal(login.origin, "https://demo.example"); assert.equal(login.pathname, "/login");
  assert.equal(login.searchParams.get("next"), "/?video=source#outputs");
  assert.deepEqual(calls, [["/api/generate", "POST"], ["/api/activity", "GET"]]);
  assert.equal(JSON.parse(storage.get(storageKey))[0].run_id, "existing");
});

test("login return locations cannot become protocol-relative external redirects", () => {
  const { context, tracker } = harness(), destinations = [];
  context.URL = URL;
  context.window.location = { href: "https://demo.example//external.example/path", replace: (url) => destinations.push(url) };
  tracker.redirectToLogin();
  assert.equal(new URL(destinations[0], "https://demo.example").searchParams.get("next"), "/");
});

function timedLocalJobHarness(read) {
  const h = harness(), calls = [], waits = []; let now = 0;
  h.context.testNow = () => now;
  h.context.testWait = async (milliseconds) => { waits.push(milliseconds); now += milliseconds; };
  h.context.fetch = async (url, init = {}) => {
    calls.push([init.method || "GET", url, now]);
    const result = init.method === "POST" ? { job_id: "quick" } : read(now);
    return { ok: true, status: 200, json: async () => result };
  };
  vm.runInContext("const originalPollGeneration = pollGeneration; pollGeneration = (record, config) => originalPollGeneration(record, { ...config, now: testNow, wait: testWait });", h.context);
  return { ...h, calls, waits, now: () => now };
}

test("a one-second local task is shown within 1.5 seconds with one submission", async () => {
  const h = timedLocalJobHarness((time) => time >= 1000
    ? { status: "complete", result: { id: "ready-video" } } : { status: "running" });
  const result = await h.tracker.job("/api/import", { video_id: "source" }, "Preparing your video…");
  assert.equal(result.id, "ready-video");
  assert.ok(h.now() >= 1000 && h.now() < 1500);
  assert.deepEqual(h.waits, [250, 250, 500]);
  assert.equal(h.calls.filter(([method]) => method === "POST").length, 1);
});

test("long local tasks and reconnects back off to a three-second maximum without resubmission", async () => {
  const long = timedLocalJobHarness((time) => time >= 20000 ? { status: "complete", result: {} } : { status: "running" });
  await long.tracker.job("/api/import", { video_id: "source" }, "Preparing your video…");
  assert.equal(Math.max(...long.waits), 3000);
  assert.ok(long.waits.filter((delay) => delay === 3000).length >= 3);
  assert.equal(long.calls.filter(([method]) => method === "POST").length, 1);
  let attempts = 0;
  const reconnect = timedLocalJobHarness(() => {
    if (++attempts <= 5) throw new TypeError("Server restarting");
    return { status: "complete", result: {} };
  });
  await reconnect.tracker.job("/api/frame", { video_id: "source" }, "Saving this frame…");
  assert.deepEqual(reconnect.waits, [1000, 2000, 3000, 3000, 3000]);
  assert.equal(reconnect.calls.filter(([method]) => method === "POST").length, 1);
});

test("generation polling keeps its existing three-second cadence", async () => {
  const { tracker } = harness(), delays = []; let time = 0;
  const result = await tracker.pollGeneration({ job_id: "generation" }, {
    request: async () => ({ status: time >= 6000 ? "complete" : "running" }),
    now: () => time, wait: async (milliseconds) => { delays.push(milliseconds); time += milliseconds; }, onStatus: () => {},
  });
  assert.equal(result.status, "complete");
  assert.deepEqual(delays, [3000, 3000]);
});

test("edit timing uses the source-bounded tail and reaches the last selectable frame", () => {
  const { context, tracker, element } = harness();
  vm.runInContext(`state={projects:[{id:'source',media:{duration:9,fps:30},
    frame_selection:{min_time:4,max_time:8.9666666667},
    candidates:[{id:'frame',freeze_time:7,frame_url:'/frame.png',cue:'Full video',
      edit_window:{actual_tail_seconds:1.9666666667,final_seconds:11.9666666667}}]}],
    defaults:{lead_seconds:4,orbit_seconds:6,tail_seconds:10}};
    activeId='frame';renderActionLabelStatus=()=>{};renderActive();`, context);
  assert.equal(element("scrub").min,4);
  assert.equal(element("scrub").max,8.9666666667);
  assert.equal(element("timing-summary").textContent,"4s action + 6s generated moment + 1.97s resumed action = 11.97s");
  vm.runInContext("state.projects[0].candidates[0].edit_window.actual_tail_seconds=0",context);
  tracker.updateTiming();
  assert.match(element("timing-summary").textContent,/0s resumed action = 10s$/);
});

test("new defaults are shown as a maximum until a saved source window is available",()=>{
  const { context,tracker,element }=harness();
  vm.runInContext("state={projects:[],defaults:{lead_seconds:4,orbit_seconds:6,tail_seconds:10}}",context);
  tracker.updateTiming();
  assert.equal(element("timing-summary").textContent,"4s action + 6s generated moment + up to 10s resumed action = up to 20s");
});

test("output review labels real silence and keeps historical edit timing",()=>{
  const { context,tracker,element }=harness();
  vm.runInContext("state={projects:[{id:'source',media:{width:1920,height:1080},video_url:'/source.mp4',generations:[]}],generation:{ready:true},defaults:{lead_seconds:4,orbit_seconds:6,tail_seconds:10}};viewedOutputs.set('source','prior')",context);
  const clip={id:'prior',test_number:1,freeze_time:40,url:'/prior.mp4',media:{duration:18},lead_seconds:8,tail_seconds:4,
    audio_provenance:{orbit:'source_slow_motion_loop',source_audio:{source_window_state:'no_audio_stream'}}};
  tracker.renderOutputViewer([clip]);
  assert.match(element('output-summary').textContent,/8s original action \+ 6s generated moment \+ 4s continued play/);
  assert.match(element('output-audio').textContent,/stays silent/);
  clip.audio_provenance.source_audio={source_window_state:'partial_audio_eof',silence_reason:'silent_source_window'};
  tracker.renderOutputViewer([clip]);
  assert.match(element('output-audio').textContent,/stays silent/);
  clip.lead_seconds=4;clip.tail_seconds=10;clip.actual_tail_seconds=0;
  tracker.renderOutputViewer([clip]);
  assert.match(element('output-summary').textContent,/0s continued play/);
});
