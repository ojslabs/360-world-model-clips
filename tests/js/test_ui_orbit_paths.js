"use strict";
const test=require("node:test"), assert=require("node:assert/strict"), fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const storageKey="world-model-clips.orbit-paths.v1";
function harness(saved={}) {
  const elements=new Map(), storage=new Map([[storageKey,JSON.stringify(saved)]]),requests=[];
  const element=(id)=>{
    if(!elements.has(id))elements.set(id,{value:"",paused:true,dataset:{},style:{},listeners:{},children:[],
      addEventListener(name,fn){this.listeners[name]=fn;},setAttribute(name,value){this[name]=value;},getAttribute(name){return this[name]??null;},removeAttribute(name){delete this[name];},
      replaceChildren(...values){this.children=[...values];},append(...values){this.children.push(...values);},pause(){},load(){},
    });return elements.get(id);
  };
  const context=vm.createContext({document:{getElementById:element,createElement:tag=>element(tag+elements.size),addEventListener(){}},window:{addEventListener(){}},
    localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
    fetch:()=>new Promise(()=>{}),AbortController,setTimeout:()=>1,clearTimeout(){},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../../ui/app.js"),"utf8"),context);
  const evaluate=code=>vm.runInContext(code,context);
  evaluate(`projectId='source';activeId='frame';state={projects:[{id:'source',media:{fps:30},candidates:[{id:'frame',freeze_time:41,frame_url:'/frame.png'}],generations:[]}],
    orbit_paths:{default:'around',note:'Relative to the selected image.',options:[
      {id:'around',label:'Around',description:'Side loop'},
      {id:'over-under',label:'Over & under',description:'Vertical loop',experimental:true,note:'The view may flip near the top or bottom.'},
      {id:'diagonal',label:'Diagonal',description:'Tilted loop'}]},generation:{ready:true},defaults:{lead_seconds:8,orbit_seconds:6,tail_seconds:4}};
    refresh=async()=>{};renderSelection=()=>{};monitorGeneration=async()=>{};
    globalThis.orbitUI={chosenOrbitPath,chooseOrbitPath,renderOrbitChoices,submitGeneration,readGeneration,finishedClips,outputLabel};`);
  let responder=async()=>({job_id:"job",run_id:"run"});
  context.testApi=async(url,data)=>{requests.push({url,data});return responder(url,data);};evaluate("api=testApi");
  return{ui:context.orbitUI,evaluate,element,storage,requests,respond:fn=>{responder=fn;}};
}

test("three source-defined move buttons choose one path without a network call",()=>{
  const h=harness();h.ui.renderOrbitChoices();const buttons=h.element("orbit-choices").children;
  assert.equal(buttons.length,3);assert.equal(buttons[0]["aria-pressed"],"true");
  buttons[1].listeners.click();assert.equal(h.ui.chosenOrbitPath().id,"over-under");
  assert.equal(buttons[1]["aria-pressed"],"true");assert.equal(buttons[0]["aria-pressed"],"false");
  assert.match(h.element("orbit-choice-note").textContent,/may flip/);assert.deepEqual(h.requests,[]);
});

test("periodic renders preserve focusable button instances and the selected move",()=>{
  const h=harness();h.ui.renderOrbitChoices();h.ui.chooseOrbitPath("diagonal");
  const button=h.element("orbit-choices").children[2];h.ui.renderOrbitChoices();
  assert.equal(h.element("orbit-choices").children[2],button);assert.equal(button["aria-pressed"],"true");
  assert.equal(h.ui.chosenOrbitPath().id,"diagonal");
});

test("Generate snapshots the selected path even when another move is chosen while posting",async()=>{
  const h=harness();h.ui.chooseOrbitPath("over-under");let release;
  h.respond(()=>new Promise(resolve=>{release=resolve;}));
  const request=h.ui.submitGeneration();h.ui.chooseOrbitPath("diagonal");
  assert.equal(h.requests.length,1);assert.equal(h.requests[0].data.orbit_path,"over-under");
  assert.equal(h.requests[0].data.item_id,"frame");
  release({job_id:"job",run_id:"run"});await request;
  assert.equal(h.evaluate("pendingGenerations.get('source').orbit_path"),"over-under");
  assert.equal(h.ui.chosenOrbitPath().id,"diagonal");
});

test("Rerun requests the chosen new move using the prior immutable frame",async()=>{
  const h=harness();h.ui.chooseOrbitPath("diagonal");await h.ui.submitGeneration({referenceRunId:"original-run",freezeTime:41});
  assert.equal(h.requests[0].data.reference_run_id,"original-run");assert.equal(h.requests[0].data.orbit_path,"diagonal");
  assert.equal(h.requests[0].data.item_id,undefined);assert.equal(h.requests.length,1);
});

test("ambiguous acknowledgement recovery matches the saved path as well as source time",async()=>{
  const h=harness(),record={video_id:"source",known_run_ids:[],freeze_time:41,orbit_path:"diagonal"},reads=[];
  const result=await h.ui.readGeneration(record,async url=>{reads.push(url);return url==="/api/state"?{projects:[{id:"source",generations:[
    {id:"wrong",provider:"Fal",freeze_time:41,orbit_path:"around"},{id:"right",provider:"Fal",freeze_time:41,orbit_path:"diagonal"}]}]}:{status:"complete"};});
  assert.equal(record.run_id,"right");assert.equal(result.status,"complete");assert.deepEqual(reads,["/api/state","/api/runs/source/right"]);
});

test("saved move choices survive reload and stay separate for each video",()=>{
  const h=harness();h.ui.chooseOrbitPath("diagonal");
  const next=harness(JSON.parse(h.storage.get(storageKey)));assert.equal(next.ui.chosenOrbitPath().id,"diagonal");
  next.evaluate("projectId='other'");assert.equal(next.ui.chosenOrbitPath().id,"around");
  next.ui.chooseOrbitPath("over-under");next.evaluate("projectId='source'");assert.equal(next.ui.chosenOrbitPath().id,"diagonal");
});

test("unsupported stored or clicked paths cannot be submitted instead of a valid choice",()=>{
  const h=harness({source:"unpublished-move"});assert.equal(h.ui.chosenOrbitPath().id,"around");
  h.ui.chooseOrbitPath("not-a-path");assert.equal(h.ui.chosenOrbitPath().id,"around");assert.equal(h.requests.length,0);
});

test("output labels describe the recorded move independently of the next generation choice",()=>{
  const h=harness();h.evaluate(`state.projects[0].generations=[{id:'previous',provider:'Fal',status:'complete',freeze_time:41,orbit_path:'over-under',orbit_path_label:'Over & under',composites:[{id:'clip',media:{duration:18}}]}]`);
  h.ui.chooseOrbitPath("around");const clip=h.ui.finishedClips()[0];
  assert.equal(clip.orbit_path_label,"Over & under");assert.match(h.ui.outputLabel(clip),/Over & under/);assert.equal(h.ui.chosenOrbitPath().id,"around");
});
