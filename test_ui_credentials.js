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
  vm.runInContext(fs.readFileSync(path.join(__dirname,"ui/app.js"),"utf8"),context);
  const evaluate = (code) => vm.runInContext(code,context);
  evaluate(`state={projects:[],jobs:[],generation:{ready:false},fal_credentials:{configured:false}};
    refresh=async()=>{}; renderSelection=()=>{}; renderOutputViewer=()=>{};
    globalThis.credentialsUI={connectFalKey,disconnectFalKey,renderFalCredentials,applyAppUpdateWhenIdle};`);
  let respond = async () => ({configured:true,verified:true});
  context.credentialRequest = async (url, data) => { requests.push({url,data,input:element("fal-key").value}); return respond(url,data); };
  evaluate("api=credentialRequest");
  return {ui:context.credentialsUI,element,evaluate,requests,writes,respond:(fn)=>{respond=fn;}};
}

test("the published form starts empty and masked, with no browser persistence field", () => {
  const html=fs.readFileSync(path.join(__dirname,"ui/sections/00_editor.html"),"utf8");
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
