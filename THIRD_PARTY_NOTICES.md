# Third-party components

The application and included `vendor/reportkit` code are provided by OJS Labs under
the [MIT license](LICENSE). No commercial font files, user videos, generated
outputs, API keys or model weights are distributed in this source repository.
The interface uses system fonts.

Setup installs the pinned packages in [requirements.txt](requirements.txt).
Their distributions retain their own licenses and dependency notices:

| Component | Upstream license information |
| --- | --- |
| yt-dlp and its installed extras | [yt-dlp licensing](https://github.com/yt-dlp/yt-dlp#license) |
| NumPy | [NumPy license](https://github.com/numpy/numpy/blob/main/LICENSE.txt) |
| SciPy | [SciPy license](https://github.com/scipy/scipy/blob/main/LICENSE.txt) |
| Pillow | [Pillow license](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| ONNX Runtime | [ONNX Runtime license](https://github.com/microsoft/onnxruntime/blob/main/LICENSE) |
| Reactor SDK 1.5.1 | Apache-2.0, retained in the installed [SDK distribution](https://pypi.org/project/reactor-sdk/1.5.1/) |
| Reactor browser SDK 3.0.2 and X2 wrapper 1.0.0 | Apache-2.0 and MIT; full bundled notices in [browser notices](ui/assets/reactor-live.NOTICES.txt) |
| FFmpeg and enabled codec libraries | [FFmpeg licensing](https://ffmpeg.org/legal.html) |
| Node.js | [Node.js license](https://github.com/nodejs/node/blob/main/LICENSE) |

The explicit speech-model setup downloads Silero VAD from commit
`3f0c9ead5490e20f6a2ce7e9d16a53af9e0e5818` and verifies SHA256
`597d30b3ec076608d059477bb14cfeffdf951bf5cae370d38f65d33bbfe82004`.
Its [upstream MIT license](https://github.com/snakers4/silero-vad/blob/3f0c9ead5490e20f6a2ce7e9d16a53af9e0e5818/LICENSE)
is retained below. The Docker build installs that same checked model in the image.

## Silero VAD

MIT License

Copyright (c) 2020-present Silero Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
