# Desktop third-party components

Frisket's license does not replace the licenses of the programs shipped beside it.
The native archive versions, download URLs and SHA-256 digests are recorded in
`native-assets.json`, which is also included in the application resources.

- **uv 0.11.29**: Astral; MIT or Apache-2.0. License texts are in `licenses/`.
  Source: https://github.com/astral-sh/uv/tree/0.11.29
- **Deno 2.9.6**: Deno contributors; MIT. License text is in `licenses/`.
  Source: https://github.com/denoland/deno/tree/v2.9.6
- **FFmpeg and FFprobe 9.0.1**: FFmpeg contributors and the libraries in Martin
  Riedl's macOS arm64 release build. This build enables GPL components, including
  x264/x265, and is distributed under GPL version 3 or later, not Frisket's license.
  The GPL text is in `licenses/GPL-3.0.txt`.
  FFmpeg source: https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.xz
  Build scripts and dependency source locations: https://git.martin-riedl.de/ffmpeg/build-script
  Exact component versions: https://ffmpeg.martin-riedl.de/download/macos/arm64/1787073674_9.0.1/versions.txt
  Distributor and corresponding-source information: https://ffmpeg.martin-riedl.de/
- **Electron**: Electron contributors; MIT, with Chromium and other component
  notices shipped by electron-builder in the application bundle.
  Source: https://github.com/electron/electron/tree/v44.3.0

Private Python and Python packages are downloaded on first launch. Their licenses
and package metadata remain with that managed runtime. The bundled default local
model cache contains the revision-pinned Whisper Base (MIT), Parakeet TDT
(CC-BY-4.0), and Silero VAD (MIT) snapshots named in Frisket's artifact manifest,
plus RapidOCR's default PP-OCRv5 recognizer and its packaged detector/classifier
aliases under their upstream terms. The persistent cache may hold additional
user-provisioned models. This beta does not bundle GPU model weights or a GPU
runtime.
