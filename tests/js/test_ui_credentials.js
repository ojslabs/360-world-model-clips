"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm"), path = require("node:path");
function harness() {
  const elements = new Map(), requests = [], writes = [];
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, { value:"", type:"password", paused:true, dataset:{}, style:{}, listeners:{},
      addEventListener(name, fn) { this.listeners[name] = fn; },
      setAttribute(name, value) { this[name] = value; }, getAttribute(name) { return this[name] ?? null; },
      removeAttribute(name) { delete this[name]; }, replaceChildren() {}, append() {}, pause() {}, load() {}, focus() {},
    });
    return elements.get(id);
  };
  const context = vm.createContext({ document:{ getElementById:element,createElement:element,addEventListener(){} },
    window:{addEventListener(){},location:{replace(){requests.push({reload:true});}}},
    localStorage:{getItem:()=>null,setItem:(...args)=>writes.push(args),removeItem(){}},
    fetch:()=>new Promise(()=>{}),AbortController,setTimeout:()=>1,clearTimeout(){},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../../ui/app.js"),"utf8"),context);
  const evaluate = (code) => vm.runInContext(code,context);
  evaluate(`state={projects:[],jobs:[],generation:{ready:false},fal_credentials:{configured:false}};
    refresh=async()=>{}; renderSelection=()=>{}; renderOutputViewer=()=>{};
    globalThis.credentialsUI={connectFalKey,disconnectFalKey,renderFalCredentials,applyAppUpdateWhenIdle};
    renderRemixes=()=>{};loadRemixPresets=async()=>{};globalThis.reactorStops=0;stopLivePreview=()=>{reactorStops++;};
    globalThis.reactorUI={connectReactorKey,disconnectReactorKey,renderReactorCredentials,reactorReady};`);
  let respond = async () => ({configured:true,verified:true});
  context.credentialRequest = async (url, data) => { requests.push({url,data,input:element("fal-key").value}); return respond(url,data); };
  evaluate("api=credentialRequest");
  return {ui:context.credentialsUI,reactor:context.reactorUI,element,evaluate,requests,writes,respond:(fn)=>{respond=fn;}};
}

test("the published form starts empty and masked, with no browser persistence field", () => {
  const html=fs.readFileSync(path.join(__dirname, "../../ui/sections/00_editor.html"),"utf8");
  const input=html.match(/<input id="fal-key"[^>]+>/)[0];
  assert.match(input,/type="password"/); assert.match(input,/autocomplete="off"/);
  assert.doesNotMatch(input,/\bvalue=/);
  const h=harness(); h.ui.renderFalCredentials();
  assert.equal(h.element("fal-key").value,""); assert.equal(h.element("fal-key-badge").textContent,"Not connected");
});

test("connect clears the visible input before sending one explicit credential request and never starts generation", async () => {
  const h=harness(); h.element("fal-key").value="user-test-key-only";
  let release; h.respond(()=>new Promise(resolve=>{release=resolve;}));
  const connecting=h.ui.connectFalKey(); await h.ui.connectFalKey();
  assert.equal(h.requests.length,1); assert.equal(h.requests[0].url,"/api/credentials/fal");
  assert.equal(h.requests[0].input,""); assert.equal(h.requests[0].data.key,"user-test-key-only");
  assert.equal(h.element("fal-key-connect").disabled,true);
  release({configured:true,verified:true}); await connecting;
  assert.equal(h.element("fal-key-form").hidden,true);
  assert.equal(h.element("fal-key-badge").textContent,"Connected");
  assert.deepEqual(h.writes,[]);
});

test("failed connection never reflects a provider message containing the submitted key", async () => {
  const h=harness(); h.element("fal-key").value="secret-that-must-not-render";
  h.respond(async()=>{const error=new Error("Rejected secret-that-must-not-render");error.status=400;throw error;});
  await h.ui.connectFalKey();
  assert.equal(h.element("fal-key").value,""); assert.equal(h.element("fal-key").type,"password");
  assert.doesNotMatch(h.element("fal-key-status").textContent,/secret-that/);
  assert.equal(h.element("fal-key-badge").textContent,"Not connected");
  assert.equal(h.element("fal-key-connect").disabled,false);
});

test("disconnect removes readiness without cancelling or resubmitting an existing job", async () => {
  const h=harness(); h.evaluate("state.fal_credentials={configured:true};state.generation={ready:true};state.action_labeling={ready:true}");
  h.respond(async()=>({configured:false})); await h.ui.disconnectFalKey();
  assert.deepEqual(h.requests.map(r=>r.url),["/api/credentials/fal/clear"]);
  assert.equal(h.evaluate("state.generation.ready"),false);
  assert.equal(h.evaluate("state.action_labeling.ready"),false);
  assert.equal(h.element("fal-key-badge").textContent,"Not connected");
  assert.match(h.element("fal-key-status").textContent,/already submitted will finish/);
});

test("a failed disconnect keeps the existing connection visible and reports uncertainty", async () => {
  const h=harness(); h.evaluate("state.fal_credentials={configured:true};state.generation={ready:true}");
  h.respond(async()=>{throw new Error("offline");}); await h.ui.disconnectFalKey();
  assert.equal(h.element("fal-key-badge").textContent,"Connected");
  assert.match(h.element("fal-key-status").textContent,/Could not confirm disconnection/);
});

test("show, change and cancel never restore a submitted key", async () => {
  const h=harness(); h.element("fal-key").value="temporary-test-value";
  h.element("fal-key-reveal").listeners.click(); assert.equal(h.element("fal-key").type,"text");
  await h.ui.connectFalKey(); assert.equal(h.element("fal-key").type,"password");
  h.element("fal-key-change").listeners.click(); assert.equal(h.element("fal-key").value,"");
  h.element("fal-key").value="abandoned-test-value";
  h.element("fal-key-cancel").listeners.click(); assert.equal(h.element("fal-key").value,"");
  assert.equal(h.requests.length,1);
});

test("automatic app updates wait while a key is pasted or its connection is pending", async () => {
  const h=harness(); h.evaluate("availableAppBuild='new-version'");
  h.element("fal-key").value="draft-test-value";
  assert.equal(h.ui.applyAppUpdateWhenIdle(),false);
  let release; h.respond(()=>new Promise(resolve=>{release=resolve;}));
  const connecting=h.ui.connectFalKey();
  assert.equal(h.ui.applyAppUpdateWhenIdle(),false);
  assert.ok(!h.requests.some(r=>r.reload));
  release({configured:true}); await connecting;
});


test("Reactor starts blank, masked and independently disconnected",()=>{
  const html=fs.readFileSync(path.join(__dirname, "../../ui/sections/00_editor.html"),"utf8");
  const input=html.match(/<input id="reactor-key"[^>]+>/)[0];
  assert.match(input,/type="password"/);assert.match(input,/autocomplete="off"/);assert.doesNotMatch(input,/\bvalue=/);
  const h=harness();h.evaluate("state.fal_credentials={configured:true}");h.reactor.renderReactorCredentials();
  assert.equal(h.element("reactor-key-badge").textContent,"Not connected");assert.equal(h.element("reactor-key").value,"");
  assert.equal(h.reactor.reactorReady(),false);
});

test("Reactor connect sends one credential request, clears the key and does not start a paid operation",async()=>{
  const h=harness();h.element("reactor-key").value="rk_offline_test_key";
  h.evaluate("state.fal_credentials={configured:true};remixCatalog={configured:false}");
  let release;h.respond(()=>new Promise(resolve=>{release=resolve;}));
  const pending=h.reactor.connectReactorKey();await h.reactor.connectReactorKey();
  assert.equal(h.requests.length,1);assert.equal(h.requests[0].url,"/api/credentials/reactor");
  assert.equal(h.element("reactor-key").value,"");assert.equal(h.element("reactor-key-connect").disabled,true);
  release({configured:true,verified:true});await pending;
  assert.equal(h.element("reactor-key-connected").hidden,false);assert.equal(h.reactor.reactorReady(),true);
  assert.equal(h.evaluate("state.fal_credentials.configured"),true);assert.deepEqual(h.writes,[]);
});

test("failed Reactor connect cannot echo an upstream secret",async()=>{
  const h=harness();h.element("reactor-key").value="rk_should_never_render";
  h.respond(async()=>{const error=new Error("rk_should_never_render");error.status=400;throw error;});
  await h.reactor.connectReactorKey();assert.doesNotMatch(h.element("reactor-key-status").textContent,/rk_should/);
  assert.equal(h.element("reactor-key").value,"");assert.equal(h.reactor.reactorReady(),false);
});

test("Reactor disconnect stops live preview and leaves Fal and submitted jobs alone",async()=>{
  const h=harness();h.evaluate("state.reactor_credentials={configured:true};state.fal_credentials={configured:true};state.generation={ready:true};remixCatalog={configured:true};state.jobs=[{id:'already-submitted',status:'running'}]");
  h.respond(async()=>({configured:false}));await h.reactor.disconnectReactorKey();
  assert.equal(h.reactor.reactorReady(),false);assert.equal(h.evaluate("reactorStops"),1);
  assert.equal(h.evaluate("state.generation.ready && state.fal_credentials.configured"),true);
  assert.equal(h.evaluate("state.jobs[0].status"),"running");assert.equal(h.requests.length,1);
  assert.equal(h.requests[0].url,"/api/credentials/reactor/clear");assert.deepEqual(h.writes,[]);
});

test("Reactor key show, change and cancel do not restore a submitted key",async()=>{
  const h=harness();h.element("reactor-key").value="rk_offline_test_key";
  h.element("reactor-key-reveal").listeners.click();assert.equal(h.element("reactor-key").type,"text");
  await h.reactor.connectReactorKey();assert.equal(h.element("reactor-key").type,"password");
  h.element("reactor-key-change").listeners.click();h.element("reactor-key").value="rk_abandoned";
  h.element("reactor-key-cancel").listeners.click();assert.equal(h.element("reactor-key").value,"");
  assert.equal(h.requests.length,1);
});

test("a stale catalog cannot authorize Reactor after its browser key is gone",()=>{
  const h=harness();h.evaluate("state.reactor_credentials={configured:false};remixCatalog={configured:true}");
  assert.equal(h.reactor.reactorReady(),false);
});

test("automatic updates wait while a Reactor key is pasted or connecting",async()=>{
  const h=harness();h.evaluate("availableAppBuild='new-version'");h.element("reactor-key").value="rk_draft_key";
  assert.equal(h.ui.applyAppUpdateWhenIdle(),false);
  let release;h.respond(()=>new Promise(resolve=>{release=resolve;}));const connecting=h.reactor.connectReactorKey();
  assert.equal(h.ui.applyAppUpdateWhenIdle(),false);assert.ok(!h.requests.some(r=>r.reload));
  release({configured:true});await connecting;
});


test("catalog loading before account state cannot leave a connected Reactor user disabled",()=>{
  const h=harness();h.evaluate("remixCatalog={configured:false};state.reactor_credentials={configured:true}");
  assert.equal(h.reactor.reactorReady(),true);
});

test("Reactor disconnect stops live immediately even when the credential clear fails",async()=>{
  const h=harness();h.evaluate("state.reactor_credentials={configured:true};remixCatalog={configured:true}");
  let fail;h.respond(()=>new Promise((_,reject)=>{fail=reject;}));const pending=h.reactor.disconnectReactorKey();
  assert.equal(h.evaluate("reactorStops"),1);assert.equal(h.reactor.reactorReady(),false);
  fail(new Error("offline"));await pending;
  assert.equal(h.evaluate("reactorStops"),1);assert.equal(h.evaluate("state.reactor_credentials.configured"),true);
  assert.match(h.element("reactor-key-status").textContent,/Could not confirm disconnection/);
});
