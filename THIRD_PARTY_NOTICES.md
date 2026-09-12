# Third-party notices

This notice covers software copied into Frisket's first-party container image.
It does not change the Apache-2.0 license for Frisket's own source code.

## PP-OCRv5 Chinese mobile recognizer

- **Component:** `ch_PP-OCRv5_rec_mobile.onnx`, used by the bundled RapidOCR
  default recognizer.
- **Source:** RapidAI's RapidOCR v3.8.0 ModelScope artifact:
  <https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.8.0/onnx/PP-OCRv5/rec/ch_PP-OCRv5_rec_mobile.onnx>
- **SHA-256:**
  `5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5`.
- **License:** `Apache-2.0`, as published by the
  [RapidOCR model repository](https://www.modelscope.cn/models/RapidAI/RapidOCR/tree/master/onnx/PP-OCRv5/rec).

Frisket verifies this checksum before atomically publishing the first-use
cache entry. The Docker image prewarms the same exact model.

## Public Suffix List

- **Component:** Mozilla Public Suffix List (`src/frisket/data/public_suffix_list.dat`),
  vendored for URL classification and shipped inside the Python wheel.
- **Source:** <https://publicsuffix.org/list/public_suffix_list.dat>
- **License:** `MPL-2.0`. The vendored file retains its original license
  header, source URL, and version/commit lines, satisfying the MPL-2.0
  notice-preservation requirement.

## ExifTool

- **Component:** ExifTool / Image-ExifTool
- **Upstream:** <https://exiftool.org/>
- **Copyright:** Copyright 2003-2026, Phil Harvey
- **Bundled version:** 13.59
- **Source archive:**
  <https://sourceforge.net/projects/exiftool/files/Image-ExifTool-13.59.tar.gz/download>
- **SHA-256:**
  `668ea3acececb7235fbd0f4900e72d5f12c9b07e5c778fd36cb1e9b5828fd65a`
- **Upstream checksum record:**
  <https://exiftool.org/checksums-13.59.txt>
- **License:** `Artistic-1.0-Perl OR GPL-1.0-or-later`. ExifTool is distributed
  under the same terms as Perl: the recipient may choose the Perl Artistic
  License or GNU GPL version 1 or, at their option, any later version. The
  upstream archive states this in its `README` (it does not contain a separate
  top-level `LICENSE` file); upstream also publishes the statement at
  <https://exiftool.org/#license>. Debian's full Perl licensing terms are
  installed in the image at `/usr/share/doc/perl/copyright`, and the upstream
  ExifTool `README` is installed at `/usr/local/share/doc/exiftool/README`.

Frisket downloads the archive named above, verifies the exact SHA-256, builds
it with its upstream Perl installation procedure, and checks that
`exiftool -ver` reports `13.59`. The installed copy is present in the
first-party team Docker image at `/usr/local/bin/exiftool`. It is not copied
into the Frisket Python wheel. Extraction envelopes report the adapter version
that processed each blob. The image digest, provenance, or SBOM produced by
deployment tooling can serve as the deployment-level integrity record;
extraction does not hash the installed Perl module tree or launcher for each
row.

## FFmpeg / ffprobe

- **Component:** FFmpeg, including the `ffprobe` command used by the metadata
  extractor
- **Upstream:** <https://ffmpeg.org/>
- **Distribution:** the architecture-specific `ffmpeg` package from the Debian
  repository configured by the `python:3.12-slim` base image
- **Integrity and version:** APT authenticates Debian's signed repository
  metadata, whose checksums bind the downloaded package bytes, and the
  resulting package is fixed by the built image's layers and digest.
  `ffprobe -version` reports the installed version; the
  extraction envelope records the reported tool version.
- **License and copyright:** Debian installs the package's machine-readable
  copyright/license notice at `/usr/share/doc/ffmpeg/copyright`; applicable
  common license texts remain under `/usr/share/common-licenses` in the image.

The Python wheel does not contain FFmpeg or ffprobe. Native/custom operators
may put `ffprobe` on `PATH` or set `FRISKET_FFPROBE_PATH` to an absolute
executable path. Full metadata coverage requires a build whose input protocols
include seekable `fd`; incompatible older builds degrade honestly.
