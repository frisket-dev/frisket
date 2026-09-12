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
and package metadata remain with that managed runtime. The following model weights
are redistributed in the bundled default local cache. The persistent cache may
also hold user-provisioned models, which are not covered by these notices. This
beta does not bundle GPU model weights or a GPU runtime.

- **Whisper base (faster-whisper/CTranslate2)**: OpenAI's Whisper base model,
  converted to CTranslate2 and published by Systran as
  `Systran/faster-whisper-base` at
  [`ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66`](https://huggingface.co/Systran/faster-whisper-base/tree/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66).
  MIT; the notice text is in `licenses/models-MIT.txt`.
- **Parakeet TDT 0.6B v3 (ONNX, int8)**: NVIDIA's multilingual
  `nvidia/parakeet-tdt-0.6b-v3`, exported to ONNX and published for onnx-asr by
  `istupakov` as `istupakov/parakeet-tdt-0.6b-v3-onnx` at
  [`8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce`](https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx/tree/8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce).
  CC-BY-4.0; the shipped notice is in `licenses/models-CC-BY-4.0.txt`.
- **Silero VAD 6.2 (ONNX)**: Silero Team's voice-activity model, converted to
  ONNX and published for onnx-asr by `istupakov` as
  `istupakov/silero-vad-onnx` at
  [`b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6`](https://huggingface.co/istupakov/silero-vad-onnx/tree/b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6).
  MIT; the notice text is in `licenses/models-MIT.txt`.
- **RapidOCR default OCR cache**: RapidAI's RapidOCR 3.8.1 package supplies the
  PP-OCRv4 detector and classifier aliases. The default recognizer is
  RapidAI's ONNX export of Baidu/PaddleOCR's `ch_PP-OCRv5_rec_mobile.onnx`,
  fetched from `RapidAI/RapidOCR` at
  [`v3.8.0`](https://www.modelscope.cn/models/RapidAI/RapidOCR/tree/v3.8.0/onnx/PP-OCRv5/rec)
  and verified as
  `5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5`.
  RapidOCR and the upstream PP-OCR models are Apache-2.0; the notice text is
  in `licenses/models-Apache-2.0.txt`.
