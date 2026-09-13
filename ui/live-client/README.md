# Live video client

This optional browser adapter streams a saved clip through the existing-video
editor. Starting is explicit. Look changes reuse the same session. No source file
or saved result is overwritten.

Build with `npm ci --ignore-scripts && npm run build`; test with `npm test`.
The lockfile pins `@reactor-models/x2` 1.0.0 and `@reactor-team/js-sdk` 3.0.2.
The latter includes an Apache-2.0 WASM transport. The wrapper declares MIT in its
published manifest; its npm package does not include a copyright notice. The
standard MIT wording and package-author attribution are retained with the bundle.
`build.mjs` retains full bundled dependency licenses in `reactor-live.NOTICES.txt`.

Serve `../assets/reactor-live.js` as JavaScript and
`../assets/reactor-live-3.0.2.wasm` as `application/wasm`, behind the same app access
control. There is no CDN dependency. The only build adaptation redirects the pinned
WASM module's asset lookup to that local URL. `bundle-meta.json` lists bundled inputs.

The adapter alone requests a short-lived token from `POST /api/reactor/live-token`.
Tokens stay in browser memory. The server uses the key from this browser's Reactor
connection and holds it only in memory. An explicit start opens
one session with one token request and one `connect` call. Read-only readiness
and SDP polling can continue while that same session warms up. Stop calls `disconnect(false)`;
page exit cleanup is best effort, backed by the server token's session duration cap.

`ReactorLive.create(callbacks)` returns `start`, `setPrompt` and `stop`.
`start({sourceUrl,videoElement,prompt,sourceVideo?})` accepts same-origin media only.
`videoElement` receives the edited stream. Optional `sourceVideo` supplies source
playback and its separately controlled audio. Canvas capture preserves aspect ratio
within 1280 by 720 at 24 fps. The model consumes the newest frames for low latency.
Its actual output raster is reported separately. Source audio is not synchronized
to the delayed edited stream, so source playback starts muted.

Callbacks: `onStatus({state,elapsedMs,maxSessionSeconds,...})`, `onStream(stream)`,
`onError(Error)`, and `onPromptApplied({prompt,accepted:true,visibleVerified:false})`.
Prompt acknowledgment means accepted for a later processing block; it does not
verify that the requested look is visible. `live` is emitted after a decoded output
frame following the model's generation-start event.

Official contracts: https://www.reactor.inc/models/x2/api and
https://docs.reactor.inc/model-api-reference/x2/tutorial.
