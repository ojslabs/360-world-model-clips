"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm"), path = require("node:path");

function harness({ startPending = false } = {}) {
  const elements = new Map(), events = {}, timers = new Map(); let timerId = 0;
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, { value:"", dataset:{}, style:{}, listeners:{}, children:[], paused:true,
      addEventListener(name, fn) { this.listeners[name] = fn; },
      replaceChildren(...children) { this.children = children; }, append(...children) { this.children.push(...children); },
      setAttribute(name, value) { this[name] = value; }, getAttribute(name) { return this[name] ?? null; },
      removeAttribute(name) { delete this[name]; },
      pause() { this.paused = true; this.pauses = (this.pauses || 0) + 1; }, load() {},
    });
    return elements.get(id);
  };
  const clients = [], calls = []; let finishStart, now = 1000000;
  const startWait = startPending ? new Promise((resolve) => { finishStart = resolve; }) : Promise.resolve();
  const adapter = { create(callbacks) {
    const client = { callbacks, async start(config) { calls.push(["start", config]); await startWait; },
      async setPrompt(prompt) { calls.push(["prompt", prompt]); }, stop() { calls.push(["stop"]); } };
    clients.push(client); return client;
  } };
  const context = vm.createContext({ document:{getElementById:element,createElement:(tag)=>element(tag+elements.size),addEventListener(){}},
    window:{ReactorLive:adapter,location:{replace(){calls.push(["reload"]);}},addEventListener:(event,fn)=>{ events[event] = fn; }},
    localStorage:{getItem:()=>null,setItem(){}},fetch:()=>new Promise(()=>{}),AbortController,Date:class extends Date { static now() { return now; } },
    setTimeout:(fn,ms)=>{ const id=++timerId; timers.set(id,{fn,ms}); return id; },clearTimeout:(id)=>timers.delete(id),
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,"ui/app.js"),"utf8"),context);
  const evaluate = (code) => vm.runInContext(code,context);
  evaluate(`projectId='source'; state={reactor_credentials:{configured:true},projects:[{id:'source',candidates:[],generations:[
    {id:'run-a',provider:'Fal',status:'complete',freeze_time:10,composites:[{id:'clip-a',url:'/a.mp4',media:{duration:18}}]},
    {id:'run-b',provider:'Fal',status:'complete',freeze_time:20,composites:[{id:'clip-b',url:'/b.mp4',media:{duration:18}}]}
  ]}],jobs:[]}; viewedOutputs.set('source','clip-b');
  remixCatalogLoading=false; remixCatalog={configured:true,live_max_session_seconds:300,live_prompts:{day:'Exact daylight prompt',night:'Exact night prompt'},presets:[]};
  globalThis.liveUI={renderLivePreview,startLivePreview,stopLivePreview,changeLiveLook,applyAppUpdateWhenIdle};`);
  return {context,evaluate,ui:context.liveUI,element,clients,calls,events,timers,finishStart,advance:(ms)=>{ now+=ms; }};
}

test("live preview defaults to Night and current finished clip; selecting a look does not connect", async () => {
  const {ui,element,calls} = harness(); ui.renderLivePreview();
  assert.equal(element("reactor-live-source").value,"clip-b");
  assert.equal(element("reactor-live-night")["aria-pressed"],"true");
  await ui.changeLiveLook("day"); await ui.changeLiveLook("night");
  assert.deepEqual(calls,[]); assert.equal(element("reactor-live-start").disabled,false);
});

test("explicit Start creates one session and uses separate muted source and output videos", async () => {
  const {ui,element,calls,clients} = harness(); ui.renderLivePreview();
  element("player").currentTime=15; element("player").paused=false;
  element("output-player").currentTime=8; element("output-player").paused=false;
  await ui.startLivePreview(); await ui.startLivePreview();
  assert.equal(clients.length,1); assert.equal(calls.length,1);
  assert.equal(calls[0][1].sourceUrl,"/b.mp4"); assert.equal(calls[0][1].prompt,"Exact night prompt");
  assert.equal(calls[0][1].sourceVideo,element("reactor-live-source-video"));
  assert.equal(calls[0][1].videoElement,element("reactor-live-player"));
  assert.equal(element("reactor-live-source-video").muted,true);
  assert.equal(element("player").currentTime,15); assert.equal(element("player").pauses,undefined);
  assert.equal(element("output-player").currentTime,8); assert.equal(element("output-player").pauses,undefined);
});

test("Day and Night change one connected client's prompt without replacing or pausing its stream", async () => {
  const {ui,element,clients,calls} = harness(); ui.renderLivePreview(); await ui.startLivePreview();
  const output=element("reactor-live-player"), stream={id:"live-stream"}; output.srcObject=stream; output.paused=false;
  clients[0].callbacks.onStatus({state:"live",elapsedMs:2500,width:1472,height:832});
  await ui.changeLiveLook("day"); await ui.changeLiveLook("night");
  assert.equal(clients.length,1); assert.deepEqual(calls.slice(1),[["prompt","Exact daylight prompt"],["prompt","Exact night prompt"]]);
  assert.equal(output.srcObject,stream); assert.equal(output.paused,false); assert.equal(output.pauses,undefined);
  assert.match(element("reactor-live-status").textContent,/1472 × 832/);
  assert.doesNotMatch(element("reactor-live-status").textContent,/%/);
  clients[0].callbacks.onStatus({state:"generating",elapsedMs:3000});
  assert.equal(element("reactor-live-empty").hidden,true);
});

test("only the newest exact prompt acknowledgement updates requested-look status", async () => {
  const {ui,clients,element} = harness(); ui.renderLivePreview(); await ui.startLivePreview();
  await ui.changeLiveLook("day"); await ui.changeLiveLook("night");
  clients[0].callbacks.onPromptApplied({prompt:"Exact daylight prompt",accepted:true,visibleVerified:false});
  assert.doesNotMatch(element("reactor-live-prompt-status").textContent,/accepted/);
  clients[0].callbacks.onPromptApplied({prompt:"Exact night prompt",accepted:true,visibleVerified:false});
  assert.match(element("reactor-live-prompt-status").textContent,/Night requested.*upcoming chunk/);
  assert.doesNotMatch(element("reactor-live-prompt-status").textContent,/visually|is applied|now visible/);
});

test("Stop and pagehide disconnect once and late callbacks cannot restart or replace the next session", async () => {
  const {ui,clients,calls,events,element} = harness(); ui.renderLivePreview(); await ui.startLivePreview();
  ui.stopLivePreview(); events.pagehide();
  assert.equal(calls.filter(([kind])=>kind==="stop").length,1);
  assert.equal(element("reactor-live-stop").disabled,true);
  await ui.startLivePreview();
  clients[0].callbacks.onError(new Error("Old error"));
  clients[0].callbacks.onStatus({state:"live",message:"Stale session"});
  assert.equal(element("reactor-live-stop").disabled,false);
  assert.doesNotMatch(element("reactor-live-status").textContent,/Old error|Stale session/);
  assert.equal(clients.length,2);
});

test("server-derived five-minute watchdog stops a session even before a first picture", async () => {
  const {ui,timers,calls,element} = harness(); ui.renderLivePreview(); await ui.startLivePreview();
  const deadline=[...timers.values()].find((timer)=>timer.ms===300000); assert.ok(deadline);
  deadline.fn();
  assert.equal(calls.filter(([kind])=>kind==="stop").length,1);
  assert.match(element("reactor-live-status").textContent,/5 minutes limit/);
});

test("token metadata can shorten but never extend the absolute session deadline", async () => {
  const {ui,timers,clients,advance,element} = harness(); ui.renderLivePreview(); await ui.startLivePreview();
  advance(20000);
  clients[0].callbacks.onStatus({state:"connecting",maxSessionSeconds:120});
  assert.ok([...timers.values()].some((timer)=>timer.ms===100000));
  assert.ok(![...timers.values()].some((timer)=>timer.ms===300000));
  assert.match(element("reactor-live-limit").textContent,/2 minutes/);
  clients[0].callbacks.onStatus({state:"connecting",maxSessionSeconds:500});
  assert.ok([...timers.values()].some((timer)=>timer.ms===100000));
});

test("elapsed clock advances without new SDK events and is cleared on Stop", async () => {
  const {ui,timers,advance,element} = harness(); ui.renderLivePreview(); await ui.startLivePreview();
  advance(65000);
  [...timers.values()].find((timer)=>timer.ms===1000).fn();
  assert.match(element("reactor-live-status").textContent,/1m 05s elapsed/);
  ui.stopLivePreview();
  assert.equal(element("reactor-live-stop").disabled,true);
  assert.doesNotMatch(element("reactor-live-status").textContent,/elapsed/);
});

test("Day/Night while connecting queues the last look without closing or creating a session", async () => {
  const {ui,calls,clients,finishStart} = harness({startPending:true}); ui.renderLivePreview();
  const starting=ui.startLivePreview();
  await ui.changeLiveLook("day"); await ui.changeLiveLook("night"); await ui.changeLiveLook("day");
  assert.equal(calls.length,1); assert.equal(clients.length,1);
  finishStart(); await starting;
  assert.deepEqual(calls.slice(1),[["prompt","Exact daylight prompt"]]);
});

test("an available app update cannot reload a connecting or active live preview", async () => {
  const {ui,evaluate,calls,finishStart} = harness({startPending:true}); ui.renderLivePreview();
  evaluate("availableAppBuild='new-version';");
  const starting=ui.startLivePreview();
  assert.equal(ui.applyAppUpdateWhenIdle(),false);
  finishStart(); await starting;
  assert.equal(ui.applyAppUpdateWhenIdle(),false);
  assert.ok(!calls.some(([kind])=>kind==="reload"));
});

test("source audio requires an explicit control and affects only the live source", async () => {
  const {ui,element} = harness(); ui.renderLivePreview(); await ui.startLivePreview();
  element("player").muted=false; element("output-player").muted=false;
  element("reactor-live-audio").checked=true; element("reactor-live-audio").listeners.change();
  assert.equal(element("reactor-live-source-video").muted,false);
  assert.equal(element("player").muted,false); assert.equal(element("output-player").muted,false);
  ui.stopLivePreview(); assert.equal(element("reactor-live-source-video").muted,true);
});

test("adapter errors stop safely without automatic retry or new token requests", async () => {
  const {ui,clients,calls,element} = harness(); ui.renderLivePreview(); await ui.startLivePreview();
  clients[0].callbacks.onError(new Error("Connection lost"));
  ui.renderLivePreview(); await ui.changeLiveLook("day");
  assert.equal(clients.length,1); assert.equal(calls.filter(([kind])=>kind==="start").length,1);
  assert.match(element("reactor-live-status").textContent,/Connection lost/);
  assert.equal(element("reactor-live-stop").disabled,true);
});

test("changing imported source stops the existing live session instead of streaming the wrong clip", async () => {
  const {ui,evaluate,calls,element} = harness(); ui.renderLivePreview(); await ui.startLivePreview();
  evaluate("projectId='other'; state.projects.push({id:'other',generations:[],candidates:[]});");
  ui.renderLivePreview();
  assert.equal(calls.filter(([kind])=>kind==="stop").length,1);
  assert.match(element("reactor-live-status").textContent,/source changed/);
});
