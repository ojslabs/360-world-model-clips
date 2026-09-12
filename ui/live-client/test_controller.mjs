import test from 'node:test';
import assert from 'node:assert/strict';
import {createLiveController, isCapacityError} from './controller.mjs';

function fixture(overrides = {}) {
  const calls = {fetch: 0, instances: [], stoppedTracks: 0, prompts: []};
  const track = {stop() { calls.stoppedTracks++; }};
  const stream = {getVideoTracks: () => [track], getTracks: () => [track]};
  const video = () => ({readyState: 4, videoWidth: 3840, videoHeight: 2160, muted: true,
    play: async () => {}, pause() {}, load() {}, addEventListener() {}, removeEventListener() {},
    requestVideoFrameCallback(fn) { this.frameCallback = fn; return 1; }, cancelVideoFrameCallback() {}});
  const source = video(), output = video();
  const canvas = {captureStream(fps) { calls.captureFps = fps; return stream; },
    getContext: () => ({drawImage() {}})};
  class Model {
    constructor(options) { this.options = options; this.handlers = {}; this.state = 'disconnected'; calls.instances.push(this); }
    on(name, fn) { this.handlers[name] = fn; }
    onPromptAccepted(fn) { this.accepted = fn; }
    onGenerationStarted(fn) { this.generated = fn; }
    onMainVideo(fn) { this.output = fn; }
    async connect(token, options) {
      calls.connectOptions = options;
      calls.sessionCreates = (calls.sessionCreates || 0) + 1;
      if (overrides.readyAfter) {
        for (let poll = 1; poll <= overrides.readyAfter; poll++) {
          if (poll > this.options.maxSessionAttempts) throw new Error('Session not ready after configured readiness polls');
          calls.readinessGets = (calls.readinessGets || 0) + 1;
          this.handlers.statusChanged?.('waiting');
          await Promise.resolve();
        }
      }
      if (overrides.connect) await overrides.connect();
      this.state = 'ready'; this.output(track, stream);
    }
    getStatus() { return this.state; }
    getSessionId() { return 'test-live-session'; }
    async disconnect(value) { calls.disconnectFlag = value; this.disconnected = true; this.state = 'disconnected'; }
    async setKeepBacklog(value) { calls.backlog = value; }
    async publishSource(value) { calls.published = value; }
    async setPrompt({prompt}) {
      calls.prompts.push(prompt);
      if (overrides.rejectPrompt) return undefined;
      const value = {type: 'prompt_accepted', prompt};
      this.accepted(value); this.generated({width: 1472, height: 832});
      return value;
    }
  }
  const env = {location: {href: 'http://localhost:8476/', origin: 'http://localhost:8476'},
    document: {createElement: name => name === 'canvas' ? canvas : video()}, performance,
    setTimeout, clearTimeout, setInterval, clearInterval, addEventListener() {}, removeEventListener() {},
    fetch: async (url, options) => {
      calls.fetch++; calls.tokenOptions = options;
      if (overrides.fetch) await overrides.fetch();
      return {ok: true, json: async () => ({jwt: 'test-private-jwt', model: 'xmax/x2', max_session_seconds: 300})};
    }};
  const statuses = [], accepted = [], errors = [];
  const client = createLiveController(Model, {onStatus: value => statuses.push(value),
    onPromptApplied: value => accepted.push(value), onError: value => errors.push(value)}, env);
  const start = () => client.start({sourceUrl: '/media/saved.mp4', videoElement: output, sourceVideo: source, prompt: 'Night'});
  return {calls, client, start, output, source, statuses, accepted, errors, Model, env};
}

test('loading is inert; one explicit start and toggles reuse one session', async () => {
  const f = fixture();
  assert.equal(f.calls.fetch, 0);
  try {
    await f.start();
    f.source.muted = false;
    await f.client.setPrompt('Day');
    await f.client.setPrompt('Night');
    assert.equal(f.calls.fetch, 1);
    assert.equal(f.calls.instances.length, 1);
    assert.deepEqual(f.calls.prompts, ['Night', 'Day', 'Night']);
    assert.deepEqual(f.calls.backlog, {keep_backlog: false});
    assert.equal(f.calls.captureFps, 24);
    assert.equal(f.calls.published.contentHint, 'detail');
    assert.equal(f.calls.instances[0].options.maxSessionAttempts, 60);
    assert.equal(f.calls.connectOptions.maxAttempts, 8);
    assert.equal(f.source.muted, false);
    assert.deepEqual(f.accepted.at(-1), {prompt: 'Night', accepted: true, visibleVerified: false});
    assert.ok(!f.statuses.some(s => s.state === 'live'));
    f.output.frameCallback();
    assert.ok(f.statuses.some(s => s.state === 'live'));
  } finally { await f.client.stop(); }
  assert.equal(f.calls.disconnectFlag, false);
  assert.equal(f.output.srcObject, null);
  assert.equal(f.calls.stoppedTracks, 1);
});

test('stop during token wait prevents any session creation', async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const f = fixture({fetch: () => gate});
  const pending = f.start();
  const rejected = assert.rejects(pending, /stopped/);
  await new Promise(resolve => setTimeout(resolve, 1));
  await f.client.stop();
  release();
  await rejected;
  assert.equal(f.calls.instances.length, 0);
  assert.equal(f.calls.fetch, 1);
});

test('waiting through several readiness GETs still creates exactly one session', async () => {
  const f = fixture({readyAfter: 4});
  try {
    await f.start();
    assert.equal(f.calls.readinessGets, 4);
    assert.equal(f.calls.sessionCreates, 1);
    assert.equal(f.calls.fetch, 1);
    assert.equal(f.calls.instances.length, 1);
    assert.equal(f.calls.prompts.length, 1);
    assert.equal(f.statuses.filter(s => s.connectionStatus === 'waiting').length, 4);
  } finally { await f.client.stop(); }
});

test('stop during connect disconnects that same client without retry', async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const f = fixture({connect: () => gate});
  const pending = f.start();
  const rejected = assert.rejects(pending, /stopped/);
  await new Promise(resolve => setTimeout(resolve, 1));
  await f.client.stop();
  release();
  await rejected;
  assert.equal(f.calls.instances.length, 1);
  assert.equal(f.calls.instances[0].disconnected, true);
  assert.equal(f.calls.prompts.length, 0);
});

test('failed command acknowledgment cannot claim the look was accepted', async () => {
  const f = fixture({rejectPrompt: true});
  await assert.rejects(f.start(), /not accepted/);
  assert.equal(f.accepted.length, 0);
  assert.equal(f.calls.fetch, 1);
  assert.equal(f.calls.instances.length, 1);
  assert.equal(f.calls.instances[0].disconnected, true);
});

test('cross-origin source and simultaneous sessions are rejected before another token', async () => {
  const a = fixture(), b = fixture();
  await assert.rejects(a.client.start({sourceUrl: 'https://example.test/video.mp4', videoElement: a.output, prompt: 'Night'}), /saved video/);
  assert.equal(a.calls.fetch, 0);
  try {
    await a.start();
    await assert.rejects(b.start(), /current live preview/);
    assert.equal(b.calls.fetch, 0);
  } finally { await a.client.stop(); await b.client.stop(); }
});

test('SDK connection errors never expose a token or retry a paid start', async () => {
  const f = fixture({connect: () => { throw new Error('eyJprivate.secret.token https://secret.test/?jwt=value'); }});
  await assert.rejects(f.start(), error => !/eyJ|https:/.test(error.message));
  assert.equal(f.calls.fetch, 1);
  assert.equal(f.calls.instances.length, 1);
  assert.ok(f.errors.every(error => !/eyJ|https:/.test(error.message)));
});

test('capacity response uses a fixed message and never starts a replacement session', async () => {
  assert.equal(isCapacityError({name: 'RateLimitedError', message: 'private detail'}), true);
  const f = fixture({connect: () => { throw new Error('HTTP429 no available capacity https://private.test/session'); }});
  await assert.rejects(f.start(), {message: 'Reactor is at capacity. Start again in a moment.'});
  assert.equal(f.calls.fetch, 1);
  assert.equal(f.calls.instances.length, 1);
  assert.equal(f.calls.instances[0].disconnected, true);
});
