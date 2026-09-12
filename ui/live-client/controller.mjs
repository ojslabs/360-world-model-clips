// One explicit live session. Look changes reuse it; loading this file spends nothing.
let activeOwner = null;
class LiveError extends Error {}
const CAPACITY_MESSAGE = 'Reactor is at capacity. Start again in a moment.';
export function isCapacityError(error) {
  return /ratelimit/i.test(String(error?.name || error?.constructor?.name || '')) ||
    error?.status === 429 || /\b429\b|no available capacity|at capacity/i.test(String(error?.message || error || ''));
}

export function createLiveController(Model, callbacks = {}, env = globalThis) {
  let client = null, source = null, output = null, capture = null;
  let drawTimer = null, durationTimer = null, frameTimer = null, frameCallback = null;
  let started = 0, epoch = 0, active = false, closing = null, generated = false, firstFrame = false, maxSessionSeconds = null;
  const owner = {};
  const now = () => env.performance?.now?.() ?? Date.now();
  const notify = (name, value) => { try { callbacks[name]?.(value); } catch {} };
  const status = (state, extra = {}) => notify('onStatus', {state, elapsedMs: Math.max(0, now() - started),
    sessionId: client?.getSessionId?.() ?? undefined, maxSessionSeconds, ...extra});
  const check = id => { if (id !== epoch || !active) throw new LiveError('Live preview was stopped.'); };
  const validatePrompt = prompt => {
    if (typeof prompt !== 'string' || !prompt.trim() || prompt.length > 1000) {
      throw new LiveError('Choose a look with a description of 1 to 1000 characters.');
    }
  };

  async function stop() {
    if (closing) return closing;
    if (!active && !client) return;
    active = false;
    epoch += 1;
    env.clearInterval(drawTimer);
    env.clearTimeout(durationTimer);
    env.clearTimeout(frameTimer);
    if (output && frameCallback != null) output.cancelVideoFrameCallback?.(frameCallback);
    capture?.getTracks?.().forEach(track => track.stop());
    source?.pause?.();
    if (output) { output.pause?.(); output.srcObject = null; }
    const ended = client;
    client = null;
    env.removeEventListener?.('pagehide', onPageHide);
    closing = Promise.resolve().then(async () => {
      try { if (ended) await ended.disconnect(false); }
      catch { notify('onError', new LiveError('The live connection could not confirm that it stopped. Its session time limit still applies.')); }
      finally {
        if (activeOwner === owner) activeOwner = null;
        status('stopped');
        closing = null;
      }
    });
    return closing;
  }

  function onPageHide() { void stop(); }

  function observeOutputFrame(id) {
    if (!output?.requestVideoFrameCallback) {
      const inspect = () => {
        if (!active || id !== epoch || firstFrame) return;
        if (generated && output.readyState >= 2 && output.videoWidth > 0) markLive();
        else frameTimer = env.setTimeout(inspect, 100);
      };
      inspect();
      return;
    }
    const tick = () => {
      if (!active || id !== epoch || firstFrame) return;
      if (generated) markLive();
      else frameCallback = output.requestVideoFrameCallback(tick);
    };
    frameCallback = output.requestVideoFrameCallback(tick);
  }

  function markLive() {
    firstFrame = true;
    status('live', {width: output.videoWidth, height: output.videoHeight, firstFrameMs: now() - started});
  }

  async function waitForSource(id) {
    if (source.readyState >= 2 && source.videoWidth > 0) return;
    await new Promise((resolve, reject) => {
      const cleanup = () => { env.clearTimeout(timer); source.removeEventListener('loadeddata', ready); source.removeEventListener('error', failed); };
      const ready = () => { cleanup(); resolve(); };
      const failed = () => { cleanup(); reject(new LiveError('This video could not be opened for live preview.')); };
      const timer = env.setTimeout(() => { cleanup(); reject(new LiveError('The source video took too long to load.')); }, 20000);
      source.addEventListener('loadeddata', ready, {once: true});
      source.addEventListener('error', failed, {once: true});
    });
    check(id);
  }

  async function setPrompt(prompt) {
    validatePrompt(prompt);
    if (!active || !client || client.getStatus?.() !== 'ready') throw new LiveError('Start live preview before changing its look.');
    const id = epoch;
    let reply;
    try { reply = await client.setPrompt({prompt}); }
    catch (error) { throw new LiveError(isCapacityError(error) ? CAPACITY_MESSAGE : 'The look change could not be sent. No new session was started.'); }
    check(id);
    if (!reply || reply.type !== 'prompt_accepted') throw new LiveError('The requested look was not accepted. The live session was not restarted.');
    return {prompt: reply.prompt, accepted: true, visibleVerified: false};
  }

  async function start({sourceUrl, videoElement, prompt, sourceVideo} = {}) {
    validatePrompt(prompt);
    if (active || closing || activeOwner) throw new LiveError('Stop the current live preview before starting another.');
    const url = new URL(sourceUrl, env.location.href);
    if (url.origin !== env.location.origin || !['http:', 'https:'].includes(url.protocol)) {
      throw new LiveError('Choose a saved video from this app for live preview.');
    }
    if (!videoElement) throw new LiveError('The live preview player is unavailable.');
    const canvas = env.document.createElement('canvas');
    if (typeof canvas.captureStream !== 'function') throw new LiveError('This browser does not support live video preview. Try a current Safari or Chrome window.');
    activeOwner = owner;
    active = true;
    started = now();
    generated = firstFrame = false;
    maxSessionSeconds = null;
    const id = ++epoch;
    source = sourceVideo || env.document.createElement('video');
    output = videoElement;
    source.muted = true;
    source.playsInline = true;
    source.loop = true;
    source.preload = 'auto';
    source.src = url.href;
    env.addEventListener?.('pagehide', onPageHide);
    try {
      status('loading_source');
      source.load?.();
      await waitForSource(id);
      source.pause?.();
      check(id);
      const scale = Math.min(1, 1280 / source.videoWidth, 720 / source.videoHeight);
      canvas.width = Math.max(2, Math.floor(source.videoWidth * scale / 2) * 2);
      canvas.height = Math.max(2, Math.floor(source.videoHeight * scale / 2) * 2);
      const context = canvas.getContext('2d', {alpha: false, desynchronized: true});
      if (!context) throw new LiveError('This browser could not prepare the live video.');
      const draw = () => { if (active && source.readyState >= 2) context.drawImage(source, 0, 0, canvas.width, canvas.height); };
      draw();
      capture = canvas.captureStream(24);
      const track = capture.getVideoTracks()[0];
      if (!track) throw new LiveError('This browser did not provide a live video track.');
      track.contentHint = 'detail';
      drawTimer = env.setInterval(draw, 1000 / 24);
      status('authenticating');
      const response = await env.fetch('/api/reactor/live-token', {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'}, body: '{}'});
      if (!response.ok) throw new LiveError(response.status === 401 ? 'Sign in again to start live preview.' : 'Live preview could not get a session. Check the connection and start again.');
      const token = await response.json();
      check(id);
      if (typeof token.jwt !== 'string' || !token.jwt || token.model !== 'xmax/x2') throw new LiveError('The live session response was incomplete.');
      const seconds = Math.min(300, Math.max(1, Number(token.max_session_seconds) || 300));
      maxSessionSeconds = seconds;
      durationTimer = env.setTimeout(() => { status('ending', {message: 'Live preview reached its session time limit.'}); void stop(); }, seconds * 1000);
      // These limits count read-only readiness/SDP polls, not session creations.
      // The app calls connect once and the token permits only one session.
      client = new Model({maxSessionAttempts: 60, maxSdpAttempts: 8, readyTimeoutMs: 60000,
        controlRequestTimeoutMs: 15000, logLevel: 'off'});
      client.on('statusChanged', value => {
        if (id !== epoch || !active) return;
        if (value === 'connecting' || value === 'waiting') status('connecting', {connectionStatus: value});
        if (value === 'disconnected') { notify('onError', new LiveError('The live connection ended. Start again when you are ready.')); void stop(); }
      });
      client.on('error', error => {
        if (id !== epoch || !active) return;
        notify('onError', new LiveError(isCapacityError(error) ? CAPACITY_MESSAGE : 'The live connection reported an error. No new session was started.'));
      });
      client.onPromptAccepted(value => {
        if (id === epoch && active) notify('onPromptApplied', {prompt: value.prompt, accepted: true, visibleVerified: false});
      });
      client.onGenerationStarted(value => {
        if (id !== epoch || !active) return;
        generated = true;
        status('generating', {width: value.width, height: value.height});
      });
      client.onMainVideo((track, stream) => {
        if (id !== epoch || !active) { track.stop?.(); return; }
        output.srcObject = stream;
        output.muted = true;
        output.playsInline = true;
        void output.play().catch(() => notify('onError', new LiveError('Press play to view the live output.')));
        notify('onStream', stream);
        observeOutputFrame(id);
      });
      status('connecting');
      await client.connect(token.jwt, {maxAttempts: 8});
      check(id);
      if (client.getStatus() !== 'ready') throw new LiveError('The live connection did not become ready. No retry was submitted.');
      await client.setKeepBacklog({keep_backlog: false});
      check(id);
      source.currentTime = 0;
      await client.publishSource(track);
      check(id);
      status('streaming_source', {sourceWidth: canvas.width, sourceHeight: canvas.height});
      await setPrompt(prompt);
      check(id);
      await source.play();
      return {sessionId: client.getSessionId?.(), sourceWidth: canvas.width, sourceHeight: canvas.height, maxSessionSeconds: seconds};
    } catch (error) {
      const ownedError = isCapacityError(error) ? new LiveError(CAPACITY_MESSAGE) :
        error instanceof LiveError ? error : new LiveError('Live preview could not start. No retry was submitted.');
      if (id === epoch || !active) await stop();
      status('error', {message: ownedError.message});
      notify('onError', ownedError);
      throw ownedError;
    }
  }

  return {start, setPrompt, stop};
}
