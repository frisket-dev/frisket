#!/usr/bin/env python3
"""Offline Opus-MT -> CTranslate2 convert-and-host pipeline.

This is the frisket-maintained, OFFLINE conversion pipeline. It is NEVER shipped
to users and NEVER imported by the frisket runtime: ``torch`` + ``transformers``
are heavy, platform-fragile deps deliberately kept out of the base and every
extra (the same no-torch policy the ASR worker is built around). Conversion
happens once, here, per pair; users pull the already-converted CT2 artifact with
only ``ctranslate2`` + ``sentencepiece`` on device.

What it does, per pair ``<src>-<tgt>``:

1. Converts ``Helsinki-NLP/opus-mt-<src>-<tgt>`` to CTranslate2 via
   ``ct2-transformers-converter`` (int8), copying the SentencePiece tokenizers
   (``source.spm``/``target.spm``) alongside ``model.bin`` + ``config.json`` +
   the shared vocabulary.
2. Computes the per-file SHA256 + size and prints a ready-to-paste
   ``PinnedArtifact`` entry for ``src/frisket/ai/models/artifact_manifest.py``.
3. If ``--upload`` and an HF write token are present, creates/updates the
   ``frisket-models/opus-mt-<src>-<tgt>-ct2`` repo, uploads the folder, and
   uses the resulting commit SHA as the pinned revision + per-file source URLs.
   Without a token it stops after step 2 and reports the upload as the single
   remaining operator step (the manifest entry it prints uses a placeholder
   revision the operator replaces after upload).

This pipeline processes the **whole catalog as a batch**. There is no
hard-coded launch set; an operator runs it over whatever
pairs they want (``--pair en-es --pair de-en …`` or a file of pairs), commits the
emitted manifest entries, and the manifest grows as pairs are converted. Every
pullable ``opus-mt:`` pair is therefore pinned + checksummed ("do it correctly
out of the gate"); pairs not yet converted simply are not in the manifest.

Usage:
    python scripts/dev/convert_opus_mt.py --pair en-es --pair de-en --out ./ct2-out
    HF_TOKEN=hf_xxx python scripts/dev/convert_opus_mt.py --pair en-es --out ./o --upload

    # Re-emit a manifest entry from an already-converted local dir (no torch):
    python scripts/dev/convert_opus_mt.py --emit-entry en-es \\
        --dir ./ct2-out/opus-mt-en-es-ct2 --revision <sha>
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

# Confirm each model's license before publishing; this is only the default.
DEFAULT_LICENSE = "CC-BY-4.0"
DEFAULT_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
MANIFEST_VERSION = "2026.07.1"
# Must match artifact_manifest.FRISKET_HF_ORG — the organization the pinned
# artifact URLs resolve against.
FRISKET_HF_ORG = "frisket-models"

_CT2_FILES = (
    "model.bin",
    "config.json",
    "shared_vocabulary.json",
    "source.spm",
    "target.spm",
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def convert_pair(src: str, tgt: str, out_root: Path) -> Path:
    """Run ct2-transformers-converter for one pair. Requires torch +
    transformers + ctranslate2 in the OFFLINE build env (never on device)."""
    model_id = f"Helsinki-NLP/opus-mt-{src}-{tgt}"
    dest = out_root / f"opus-mt-{src}-{tgt}-ct2"
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ct2-transformers-converter",
        "--model",
        model_id,
        "--output_dir",
        str(dest),
        "--quantization",
        "int8",
        "--force",
        "--copy_files",
        "source.spm",
        "target.spm",
    ]
    print(f"[convert] {model_id} -> {dest}", file=sys.stderr)
    subprocess.run(cmd, check=True)  # noqa: S603 -- offline operator tool
    return dest


def checksum_dir(directory: Path) -> list[tuple[str, str, int]]:
    """Return ``[(repo_relpath, sha256, size), …]`` for the CT2 files present
    in ``directory``, in a deterministic (sorted) order."""
    out: list[tuple[str, str, int]] = []
    for name in _CT2_FILES:
        path = directory / name
        if not path.is_file():
            # Some conversions emit shared_vocabulary as .txt.
            alt = directory / name.replace(".json", ".txt")
            if name == "shared_vocabulary.json" and alt.is_file():
                path = alt
            else:
                continue
        out.append((path.name, _sha256_file(path), path.stat().st_size))
    return sorted(out)


def emit_manifest_entry(
    src: str,
    tgt: str,
    revision: str,
    files: list[tuple[str, str, int]],
    *,
    license_id: str = DEFAULT_LICENSE,
    license_url: str = DEFAULT_LICENSE_URL,
) -> str:
    """Render a ready-to-paste ``PinnedArtifact`` literal for the manifest."""
    repo = f"{FRISKET_HF_ORG}/opus-mt-{src}-{tgt}-ct2"
    lines = [
        '    "opus-mt:%s-%s": PinnedArtifact(' % (src, tgt),
        '        ref="opus-mt:%s-%s",' % (src, tgt),
        '        kind="ct2_pair",',
        '        display_name="%s \\u2192 %s",' % (src.upper(), tgt.upper()),
        '        manifest_version="%s",' % MANIFEST_VERSION,
        '        license="%s",' % license_id,
        '        license_url="%s",' % license_url,
        '        source_url="https://huggingface.co/%s/tree/%s",' % (repo, revision),
        "        files=(",
    ]
    for relpath, sha, size in files:
        url = f"https://huggingface.co/{repo}/resolve/{revision}/{relpath}"
        lines.append(
            '            PinnedFile("%s", "%s", "%s", %d),' % (relpath, url, sha, size)
        )
    lines.append("        ),")
    lines.append("    ),")
    return "\n".join(lines)


def upload_pair(directory: Path, src: str, tgt: str, token: str) -> str:
    """Create/update the frisket-models repo and upload the folder. Returns the
    commit SHA to pin. Requires ``huggingface_hub`` in the offline env."""
    from huggingface_hub import HfApi

    repo_id = f"{FRISKET_HF_ORG}/opus-mt-{src}-{tgt}-ct2"
    api = HfApi(token=token)
    api.create_repo(repo_id, repo_type="model", exist_ok=True)
    commit = api.upload_folder(
        repo_id=repo_id,
        folder_path=str(directory),
        commit_message=f"Add CT2 conversion of opus-mt-{src}-{tgt} ({MANIFEST_VERSION})",
    )
    return getattr(commit, "oid", None) or "REPLACE_WITH_COMMIT_SHA"


def _parse_pair(pair: str) -> tuple[str, str]:
    if "-" not in pair:
        raise SystemExit(f"invalid --pair {pair!r}; expected '<src>-<tgt>'")
    parts = pair.split("-")
    if len(parts) == 2:
        return parts[0], parts[1]
    raise SystemExit(f"ambiguous --pair {pair!r} (multi-subtag side); use --src/--tgt")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pair", action="append", default=[], help="a '<src>-<tgt>' pair to convert"
    )
    parser.add_argument("--src", help="explicit source subtag (with --tgt)")
    parser.add_argument("--tgt", help="explicit target subtag (with --src)")
    parser.add_argument("--out", type=Path, default=Path("./ct2-out"))
    parser.add_argument(
        "--upload", action="store_true", help="upload to frisket-models"
    )
    parser.add_argument(
        "--emit-entry", help="re-emit a manifest entry for '<src>-<tgt>' from --dir"
    )
    parser.add_argument("--dir", type=Path, help="an already-converted CT2 dir")
    parser.add_argument(
        "--revision", default="REPLACE_WITH_COMMIT_SHA", help="pinned revision"
    )
    parser.add_argument("--license", default=DEFAULT_LICENSE)
    parser.add_argument("--license-url", default=DEFAULT_LICENSE_URL)
    args = parser.parse_args(argv)

    if args.emit_entry:
        src, tgt = _parse_pair(args.emit_entry)
        directory = args.dir or (args.out / f"opus-mt-{src}-{tgt}-ct2")
        files = checksum_dir(directory)
        if not files:
            raise SystemExit(f"no CT2 files found under {directory}")
        print(
            emit_manifest_entry(
                src,
                tgt,
                args.revision,
                files,
                license_id=args.license,
                license_url=args.license_url,
            )
        )
        return 0

    pairs: list[tuple[str, str]] = [_parse_pair(p) for p in args.pair]
    if args.src and args.tgt:
        pairs.append((args.src, args.tgt))
    if not pairs:
        parser.error("supply at least one --pair or --src/--tgt")

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    entries: list[str] = []
    for src, tgt in pairs:
        directory = convert_pair(src, tgt, args.out)
        files = checksum_dir(directory)
        revision = args.revision
        if args.upload:
            if not token:
                print(
                    "[upload] no HF_TOKEN/HUGGINGFACE_TOKEN in env -- skipping upload; "
                    "the manifest entry below uses a placeholder revision. Upload the "
                    f"folder {directory} to {FRISKET_HF_ORG}/opus-mt-{src}-{tgt}-ct2 and "
                    "replace the revision.",
                    file=sys.stderr,
                )
            else:
                revision = upload_pair(directory, src, tgt, token)
                print(f"[upload] committed at {revision}", file=sys.stderr)
        entries.append(
            emit_manifest_entry(
                src,
                tgt,
                revision,
                files,
                license_id=args.license,
                license_url=args.license_url,
            )
        )

    print("\n# --- paste into src/frisket/ai/models/artifact_manifest.py _MANIFEST ---")
    print("\n".join(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
