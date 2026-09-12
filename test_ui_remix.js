"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm"), path = require("node:path");

function harness() {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: "", dataset: {}, style: {}, listeners: {}, children: [], paused: true,
      addEventListener(name, callback) { this.listeners[name] = callback; },
      replaceChildren(...items) { this.children = []; this.append(...items); },
      append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } },
      remove() { this.parent.children = this.parent.children.filter((item) => item !== this); },
      setAttribute(name, value) { this[name] = value; }, getAttribute(name) { return this[name] ?? null; },
      removeAttribute(name) { delete this[name]; },
      pause() { this.paused = true; this.pauses = (this.pauses || 0) + 1; },
      load() { this.currentTime = 0; this.loads = (this.loads || 0) + 1; },
      scrollIntoView() { this.scrolled = true; },
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: element, createElement: (tag) => element(`${tag}-${elements.size}`), addEventListener() {} },
    window: { addEventListener() {} }, localStorage: { getItem: () => null, setItem() {} },
    fetch: () => new Promise(() => {}), AbortController,
    setTimeout: (...args) => { const timer = setTimeout(...args); timer.unref(); return timer; }, clearTimeout,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "ui/app.js"), "utf8"), context);
  vm.runInContext(`
    projectId = "source";
    state = { projects: [{ id: "source", title: "Source", candidates: [], remixes: [], generations: [
      { id: "run-b", provider: "Fal", status: "complete", freeze_time: 24, composites: [{ id: "clip-b", url: "/b.mp4", media: { duration: 18 } }] },
      { id: "run-a", provider: "Fal", status: "complete", freeze_time: 12, composites: [{ id: "clip-a", url: "/a.mp4", media: { duration: 18 } }] }
    ] }], jobs: [] };
    remixCatalogLoading = false;
    remixCatalog = { configured: true, provider: "Reactor", model: "X2", presets: [
      { id: "jelly", label: "Jelly world", prompt: "Make the world jelly." },
      { id: "night", label: "Day to night", prompt: "Turn day into night." }
    ] };
    globalThis.apiRemix = { renderRemixes, renderRemixViewer, submitRemix, loadRemixPresets,
      remixDraft, renderRemixProgress, activityPresentation, renderActivity, drafts: remixDrafts, viewed: viewedRemixes };
  `, context);
  return { context, ui: context.apiRemix, element,
    evaluate: (code) => vm.runInContext(code, context) };
}
const plain = (value) => JSON.parse(JSON.stringify(value));

test("Reactor form defaults to all completed clips and the server Day to night prompt without submitting", () => {
  const { ui, element } = harness();
  element("output-player").src = "/original.mp4"; element("output-player").currentTime = 5;
  ui.renderRemixes();
  assert.equal(element("remix-source").value, "__all__");
  assert.equal(element("remix-source").children.length, 3);
  assert.equal(element("remix-preset").value, "night");
  assert.equal(element("remix-prompt").value, "Turn day into night.");
  assert.equal(element("remix-generate").disabled, false);
  assert.match(element("remix-connection").textContent, /2 clips.*Reactor/);
  assert.equal(element("output-player").src, "/original.mp4");
  assert.equal(element("output-player").currentTime, 5);
  assert.equal(element("output-player").pauses, undefined);
});

test("editable prompt and individual clip selection survive ordinary refresh", () => {
  const { ui, element } = harness(); ui.renderRemixes();
  element("remix-source").value = "clip-a"; element("remix-source").listeners.change();
  element("remix-prompt").value = "Night with warm street lights."; element("remix-prompt").listeners.input();
  const options = element("remix-source").children;
  ui.renderRemixes();
  assert.equal(element("remix-source").value, "clip-a");
  assert.equal(element("remix-source").children, options);
  assert.equal(element("remix-prompt").value, "Night with warm street lights.");
  element("remix-preset").value = "jelly"; element("remix-preset").listeners.change();
  assert.equal(element("remix-prompt").value, "Make the world jelly.");
});

test("explicit batch submit posts once and double click cannot submit again", async () => {
  const { ui, context, evaluate } = harness(); ui.renderRemixes();
  const calls = []; let resolve;
  context.api = async (url, data) => { calls.push([url, plain(data)]); return new Promise((done) => { resolve = done; }); };
  evaluate("api = globalThis.api; refresh = async () => {}; pollGeneration = async () => ({status:'complete'});");
  const before = evaluate("JSON.stringify(state.projects)");
  const first = ui.submitRemix({ preventDefault() {} });
  await ui.submitRemix({ preventDefault() {} });
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], ["/api/remix", { video_id: "source", preset_id: "night", prompt: "Turn day into night.", composite_ids: ["clip-b", "clip-a"] }]);
  resolve({ jobs: [{ job_id: "job-b", remix_id: "remix-b" }, { job_id: "job-a", remix_id: "remix-a" }] });
  await first;
  assert.equal(evaluate("JSON.stringify(state.projects)"), before);
  assert.equal(ui.remixDraft().message, "2 remixes are ready below.");
});

test("an individual remix sends only its explicitly selected completed clip", async () => {
  const { ui, context, element, evaluate } = harness(); ui.renderRemixes();
  element("remix-source").value = "clip-a"; element("remix-source").listeners.change();
  let submitted;
  context.request = async (url, data) => { submitted = plain(data); return { job_id: "one" }; };
  evaluate("api = request; refresh = async () => {}; pollGeneration = async () => ({status:'complete'});");
  await ui.submitRemix();
  assert.equal(submitted.composite_id, "clip-a"); assert.equal(submitted.composite_ids, undefined);
});

test("newly completed remixes select once and polling preserves user playback and result choice", () => {
  const { ui, element, evaluate } = harness();
  evaluate(`project().remixes = [{id:'older', composite_id:'clip-a', label:'Day to night', status:'complete', url:'/night-a.mp4', created_at:1, media:{duration:18,height:832}}];`);
  ui.renderRemixes();
  const video = element("remix-player"); video.currentTime = 7; video.paused = false;
  const pauses = video.pauses; ui.renderRemixes();
  assert.equal(video.currentTime, 7); assert.equal(video.paused, false); assert.equal(video.pauses, pauses);
  assert.match(element("remix-summary").textContent, /Reactor X2.*832p output/);
  assert.doesNotMatch(element("remix-summary").textContent, /1080/);
  evaluate(`project().remixes.push({id:'newer',composite_id:'clip-b',label:'Day to night',status:'complete',url:'/night-b.mp4',created_at:2});`);
  ui.renderRemixes(); assert.equal(video.src, "/night-b.mp4");
  element("remix-result").value = "older"; element("remix-result").listeners.change();
  video.currentTime = 4; video.paused = false; const selectedPauses = video.pauses;
  ui.renderRemixes(); assert.equal(video.src, "/night-a.mp4"); assert.equal(video.pauses, selectedPauses); assert.equal(video.currentTime, 4);
});

test("unconfigured connection and active saved remixes block submissions", async () => {
  const { ui, element, context, evaluate } = harness(); let calls = 0;
  context.request = async () => { calls++; };
  evaluate("api = request; remixCatalog.configured = false;"); ui.renderRemixes(); await ui.submitRemix();
  assert.equal(element("remix-generate").disabled, true); assert.equal(calls, 0);
  evaluate("remixCatalog.configured = true; project().remixes = [{id:'saved',status:'running',message:'Receiving frames.'}];");
  ui.renderRemixes(); await ui.submitRemix();
  assert.equal(element("remix-generate").disabled, true); assert.equal(calls, 0);
  assert.match(element("remix-progress").textContent, /Reactor.*Receiving frames/);
});

test("lost acknowledgement is not automatically resubmitted", async () => {
  const { ui, context, evaluate, element } = harness(); ui.renderRemixes(); let calls = 0;
  context.request = async () => { calls++; throw new TypeError("Connection lost"); };
  evaluate("api = request; refresh = async () => {};");
  await ui.submitRemix();
  assert.equal(calls, 1); assert.match(element("remix-progress").textContent, /No automatic retry/);
});

test("preset fetch is read-only and server prompts are preserved verbatim", async () => {
  const { ui, context, evaluate, element } = harness(), calls = [];
  context.request = async (url, data) => { calls.push([url, data]); return { configured: true, model: "X2", provider: "Reactor", presets: [{id:"night-v2",label:"Day to night",prompt:"Exact server prompt.\nKeep punctuation!"}] }; };
  evaluate("api = request;"); await ui.loadRemixPresets();
  assert.deepEqual(calls, [["/api/remix-presets", undefined]]);
  assert.equal(element("remix-prompt").value, "Exact server prompt.\nKeep punctuation!");
});

test("Reactor activity retains its provider name and measured frame count", () => {
  const { ui, element, evaluate } = harness();
  const job = { id:"reactor-job",operation:"remix",kind:"Reactor: Day to night",remix_id:"night",video_id:"source",status:"running",created_at:1,progress:{stage:"generating",percent:25,frames_received:120,frames_expected:480} };
  const view = ui.activityPresentation(job, 5000);
  assert.equal(view.title, "Reactor: Day to night"); assert.equal(view.label, "Generating with Reactor");
  assert.match(view.metrics, /120 \/ 480 frames received/); assert.equal(view.percent, 25);
  evaluate("render = () => {};"); ui.renderActivity([job], 5000);
  const card = element("activity-jobs").children[0], button = card.children.at(-1);
  button.listeners.click(); assert.equal(element("reactor-remix").scrolled, true);
});

test("inline remix progress uses live jobs started outside the UI without touching players or prompt", () => {
  const { ui, element } = harness(); ui.renderRemixes();
  element("remix-prompt").value = "My edited prompt"; element("remix-prompt").listeners.input();
  element("remix-player").src = "/prior-remix.mp4"; element("remix-player").currentTime = 4; element("remix-player").paused = false;
  const job = {id:"outside",operation:"remix",video_id:"source",status:"running",created_at:Date.now()/1000-30,progress:{stage:"remixing",percent:91.2,frames_received:394,frames_expected:432}};
  ui.renderRemixProgress([job]);
  assert.match(element("remix-progress").textContent, /91%.*394 \/ 432 frames received.*30s elapsed/);
  assert.equal(element("remix-generate").disabled, true);
  assert.equal(element("remix-prompt").value, "My edited prompt");
  assert.equal(element("remix-player").currentTime, 4); assert.equal(element("remix-player").paused, false);
});

test("a newer successful matching remix supersedes its old error without removing history", () => {
  const { ui, element, evaluate } = harness();
  evaluate(`project().remixes = [
    {id:'failed',composite_id:'clip-a',preset_id:'night',prompt:'Same prompt',status:'failed',created_at:1,error:'Old capture failed'},
    {id:'fixed',composite_id:'clip-a',preset_id:'night',prompt:'Same prompt',status:'complete',created_at:2,url:'/fixed.mp4'}
  ];`);
  const before = evaluate("JSON.stringify(project().remixes)");
  ui.renderRemixes();
  assert.equal(element("remix-progress").hidden, true);
  assert.equal(element("remix-player").src, "/fixed.mp4");
  assert.equal(evaluate("JSON.stringify(project().remixes)"), before);
  evaluate("project().remixes.push({id:'other-failure',composite_id:'clip-b',preset_id:'night',prompt:'Same prompt',status:'failed',created_at:3,error:'Other clip failed'});");
  ui.renderRemixProgress();
  assert.equal(element("remix-progress").textContent, "Other clip failed");
});

test("a different prompt, preset, source, older success or missing output cannot hide an unresolved remix error", () => {
  for (const change of ["prompt:'Different prompt'", "preset_id:'jelly'", "composite_id:'clip-b'", "created_at:0", "url:null"]) {
    const { ui, element, evaluate } = harness();
    evaluate(`project().remixes = [
      {id:'failed',composite_id:'clip-a',preset_id:'night',prompt:'Same prompt',status:'failed',created_at:1,error:'Still unresolved'},
      {id:'other',composite_id:'clip-a',preset_id:'night',prompt:'Same prompt',status:'complete',created_at:2,url:'/other.mp4',${change}}
    ];`);
    ui.renderRemixProgress();
    assert.equal(element("remix-progress").textContent, "Still unresolved", change);
  }
});
