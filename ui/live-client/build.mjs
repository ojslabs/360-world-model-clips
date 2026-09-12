import {build} from 'esbuild';
import {readFile, writeFile, copyFile, mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root = path.dirname(fileURLToPath(import.meta.url));
const assets = path.resolve(root, '../assets');
await mkdir(assets, {recursive: true});
const wasmSource = path.join(root, 'node_modules/@reactor-team/js-sdk/dist/wasm/reactor_wasm.js');
const wasmTarget = 'reactor-live-3.0.2.wasm';
const result = await build({
  absWorkingDir: root, entryPoints: ['entry.mjs'], outfile: path.join(assets, 'reactor-live.js'),
  bundle: true, format: 'iife', platform: 'browser', target: ['safari16', 'chrome110'],
  minify: true, legalComments: 'external', metafile: true,
  define: {'process.env.NODE_ENV': '"production"', 'import.meta.env.DEV': 'false'},
  plugins: [{name: 'same-origin-wasm', setup(builder) {
    builder.onLoad({filter: /reactor_wasm\.js$/}, async () => {
      const source = await readFile(wasmSource, 'utf8');
      const original = "new URL('reactor_wasm_bg.wasm', import.meta.url)";
      if (!source.includes(original)) throw new Error('Pinned SDK WASM asset lookup changed.');
      return {loader: 'js', contents: source.replace(original,
        `new URL('/assets/${wasmTarget}', globalThis.location.href)`)};
    });
  }}],
});
await copyFile(path.join(root, 'node_modules/@reactor-team/js-sdk/dist/wasm/reactor_wasm_bg.wasm'), path.join(assets, wasmTarget));
await writeFile(path.join(root, 'bundle-meta.json'), JSON.stringify(result.metafile, null, 2) + '\n');
const licenses = [
  ['@reactor-models/x2 1.0.0, author Reactor Technologies, Inc. Package manifest declares MIT.', 'licenses/MIT.txt'],
  ['@reactor-team/js-sdk 3.0.2 and its reactor-wasm transport, Reactor Technologies, Inc. Apache-2.0.', 'licenses/Apache-2.0.txt'],
  ['awaitqueue', 'node_modules/awaitqueue/LICENSE'],
  ['debug', 'node_modules/debug/LICENSE'],
  ['ms', 'node_modules/ms/license.md'],
  ['react', 'node_modules/react/LICENSE'],
  ['mp4box', 'node_modules/mp4box/LICENSE'],
];
const notices = ['Third-party notices for the bundled live client.\nThe bundle and WASM are served locally; no API keys are included.\n'];
for (const [label, file] of licenses) notices.push(label + '\n\n' + await readFile(path.join(root, file), 'utf8'));
await writeFile(path.join(assets, 'reactor-live.NOTICES.txt'), notices.join('\n\n')); 
console.log('Built local live client and pinned WASM transport.');
