"""Pinned artifact manifest.

The manifest is the single front door for every model weight frisket pins,
and it carries **two distinct integrity guarantees** -- a reader (and the
admin surface) must never conflate them:

- **checksum** (``PinnedArtifact.integrity == "checksum"``, kinds
  ``ct2_pair``/``hf_file``/``spacy_model``): a per-file SHA256, verified against the streamed
  bytes before promotion. This is the original, strongest guarantee -- a
  license, a per-file checksum, a pin version, and an exact source URL,
  recorded at pull time and again in run receipts.
- **hf_revision_pinned** (``kind == "hf_snapshot"``): a Hugging Face git
  revision (an immutable commit SHA) plus an exact expected file list, with
  NO per-file checksum. This is a deliberate, narrower guarantee, scoped to
  HF-sourced weights: the maintainer decided HF revision pinning is
  sufficient integrity for artifacts ``huggingface_hub.snapshot_download``
  already resolves by immutable revision -- building separate per-file
  checksum machinery for them (and requiring a maintainer to hand-record
  hashes) was rejected as over-engineering. An ``hf_snapshot`` entry is a
  THIN delegation to ``snapshot_download``: this module never reimplements
  HF's cache layout, content dedup, or resume.

It is a **build-time, in-tree** catalog, not a network fetch -- a
network-fetched manifest would itself be an unpinnable supply-chain hop. The
manifest *is* the pin. Updating a
pinned artifact is a code change + review + a new ``manifest_version``, exactly
like any other dependency bump; that policy is per-entry (each
``PinnedArtifact`` carries its own ``manifest_version``), so adding new
entries does not require bumping the version already recorded on the entries
that shipped before them. For the two Parakeet ``hf_snapshot`` entries the
edit site is NOT this file: their revision + file-list are built FROM the
``_workers/parakeet_model.py`` constants (imported above), and the inference
worker validates against those same constants and rejects any other revision
-- re-pinning Parakeet means changing ``parakeet_model.py``, which updates
this catalog automatically.

Gating:
- ``opus-mt:`` refs are manifest-gated -- they name a frisket-hosted CT2
  conversion, so a pair not present here is not pullable (terminal
  ``manifest_missing``). The convert-and-host pipeline
  (``scripts/dev/convert_opus_mt.py``) is what ADDS entries; there is deliberately
  no hard-coded launch set: pairs are pinned as they are converted in a
  whole-catalog batch.
- ``hf:`` refs MAY be pinned here (then they are checksum-verified); a ``hf:``
  ref NOT in the manifest is still pullable as a free-form **unpinned** artifact
  behind the "unpinned -- no checksum, no license vetting, on you"
  acknowledgment. That unpinned path does not touch this
  module.
- ``hf-snapshot:`` entries (``kind == "hf_snapshot"``) are ALWAYS
  manifest-gated -- there is no free-form unpinned path for a multi-file
  snapshot pull (a bigger blast radius than a single unpinned file, so the
  single-file carve-out does not extend to it).

The frisket HF org that hosts the pinned CT2 pairs is ``frisket-models``.
The six pair repos were transferred in place from the interim ``frisket-dev``
org via HF's move-repo API; content and per-file SHA256 pins are unchanged,
and only the org in the URL changed. Every pinned ``source``/``source_url``
below, the constant, and the
``opus_mt_source_url`` generator agree on this one org so a newly converted
pair is uploaded to and pinned at the same place it is pulled from.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from frisket.engine._workers.parakeet_model import (
    MODEL_FILES as _PARAKEET_MODEL_FILES,
    MODEL_REVISION as _PARAKEET_MODEL_REVISION,
    VAD_FILES as _PARAKEET_VAD_FILES,
    VAD_REVISION as _PARAKEET_VAD_REVISION,
)

# The org that hosts pinned frisket-converted artifacts. `frisket-models` is
# also the name of the sidecar and the Modal app elsewhere in the codebase,
# so the HF org intentionally matches them.
FRISKET_HF_ORG = "frisket-models"

# Parakeet's two upstream HF repo ids. `parakeet_model.py` is the zero-import
# leaf module shared by the worker and the artifact resolver for the
# revision + file-list constants (imported above); it deliberately has no
# repo-id constants of its own, so this manifest is their one home --
# `_workers/parakeet_artifacts.py` imports these two names from here rather
# than re-declaring the strings.
PARAKEET_MODEL_HF_REPO = "istupakov/parakeet-tdt-0.6b-v3-onnx"
PARAKEET_VAD_HF_REPO = "istupakov/silero-vad-onnx"

# faster-whisper's default 'base' size, revision-pinned the same way (kind
# "hf_snapshot", "hf_revision_pinned" integrity -- see the module docstring).
# Unlike Parakeet there is no companion "zero-import leaf" module: nothing in
# the inference worker needs to validate against these constants at import
# time, so this manifest entry IS the one edit site for re-pinning. The
# worker (`_workers/faster_whisper_worker.py`) resolves this exact snapshot
# offline from the local Hub cache when `model_size == "base"` (the
# default); other sizes are unaffected and keep today's unpinned lazy
# resolution through the library's own default.
WHISPER_BASE_HF_REPO = "Systran/faster-whisper-base"

# FastEmbed's quantized ONNX snapshot for PROVIDERLESS_CLASSIFY_MODEL.
_PROVIDERLESS_CLASSIFY_HF_REPO = "qdrant/bge-small-en-v1.5-onnx-q"
_PROVIDERLESS_CLASSIFY_HF_REVISION = "52398278842ec682c6f32300af41344b1c0b0bb2"
_PROVIDERLESS_CLASSIFY_HF_FILES = (
    "config.json",
    "model_optimized.onnx",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)


@dataclass(frozen=True)
class PinnedFile:
    """One file of a pinned artifact.

    ``repo_relpath`` is the destination path under the artifact's install dir;
    ``source`` is the fully-qualified download URL (an HF ``resolve`` URL);
    ``sha256`` is the hex pin verified against the streamed bytes; ``size`` is
    the expected byte length (drives progress + a cheap load-time presence
    check).
    """

    repo_relpath: str
    source: str
    sha256: str
    size: int


@dataclass(frozen=True)
class HfSnapshotSource:
    """Identity for a revision-pinned (NOT per-file-checksummed) HF snapshot.

    Deliberately thin: a repo id, an immutable git revision, and the exact
    file allowlist -- nothing more. This is pass-through data for
    ``huggingface_hub.snapshot_download``; it carries no URLs and no
    checksums because the integrity guarantee for this source type IS the
    pinned revision, not a re-hash of bytes HF already serves
    content-addressed. Reimplementing per-file checksums on top of this would
    be exactly the over-engineering the maintainer decision rejected.
    """

    repo_id: str
    revision: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class PinnedArtifact:
    """A pinned, licensed artifact keyed by canonical ref.

      Two source shapes share this one type, distinguished by ``kind`` and
      surfaced uniformly via ``integrity`` (see the module docstring):
    ``ct2_pair``/``hf_file``/``spacy_model`` are checksum-verified (``files`` is populated,
      ``hf_snapshot`` is None); ``hf_snapshot`` is revision-pinned (``files`` is
      empty, ``hf_snapshot`` carries the repo id/revision/file-list instead).
    """

    ref: str
    kind: str  # "ct2_pair" | "hf_file" | "hf_snapshot" | "spacy_model"
    display_name: str
    manifest_version: str
    license: str  # SPDX id, e.g. "CC-BY-4.0", "Apache-2.0"
    license_url: str
    source_url: str  # the exact HF repo@revision (or CDN) pulled
    files: tuple[PinnedFile, ...]
    hf_snapshot: HfSnapshotSource | None = None
    # Approximate download size for entries whose exact byte sizes are NOT
    # pinned (``hf_snapshot`` pins a revision + file list, not sizes) -- a
    # UI-disclosure number only, never an integrity or progress input.
    # Checksummed entries leave this None and use ``total_size``.
    approx_size_bytes: int | None = None

    @property
    def integrity(self) -> str:
        """The one visible signal that tells a reader (and the admin route)
        which of the two guarantees this entry carries: ``"checksum"``
        (per-file SHA256, verified before promotion) or
        ``"hf_revision_pinned"`` (an immutable HF git revision, no per-file
        hash). Never silently absent -- every entry answers this."""
        return "hf_revision_pinned" if self.hf_snapshot is not None else "checksum"

    @property
    def total_size(self) -> int | None:
        """Total pinned byte size, or None when unknown ahead of download
        (``hf_snapshot`` -- the file list is pinned, the sizes are not)."""
        if self.hf_snapshot is not None:
            return None
        return sum(f.size for f in self.files)

    @property
    def composite_digest(self) -> str:
        """The single digest stored in ``model_pulls.resolved_digest``.

        - ``hf_snapshot``: ``"hf-revision:" + repo_id + "@" + revision`` --
          deliberately NOT ``sha256:``-prefixed, so this can never be
          mistaken for a checksum by a reader comparing digest strings.
        - Single-file (checksummed): ``"sha256:" + files[0].sha256``.
        - Multi-file (checksummed): ``"sha256:" + sha256("\\n".join(sorted(
          f"{sha256}  {repo_relpath}" for f in files)))`` -- deterministic and
          order-independent.
        """
        if self.hf_snapshot is not None:
            return f"hf-revision:{self.hf_snapshot.repo_id}@{self.hf_snapshot.revision}"
        if len(self.files) == 1:
            return f"sha256:{self.files[0].sha256}"
        lines = sorted(f"{f.sha256}  {f.repo_relpath}" for f in self.files)
        digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
        return f"sha256:{digest}"


def opus_mt_source_url(src: str, tgt: str, revision: str, filename: str) -> str:
    """The canonical HF ``resolve`` URL for a frisket-hosted CT2 pair file.

    The convert-and-host pipeline uploads to
    ``frisket-models/opus-mt-<src>-<tgt>-ct2`` and records the exact
    ``revision`` (a git SHA) into the manifest entry it emits, so this is only
    the *shape* the pipeline follows -- the pin lives in the emitted
    ``PinnedFile.source``.
    """
    repo = f"{FRISKET_HF_ORG}/opus-mt-{src}-{tgt}-ct2"
    return f"https://huggingface.co/{repo}/resolve/{revision}/{filename}"


_MANIFEST: dict[str, PinnedArtifact] = {
    # spaCy's small English OntoNotes pipeline. The wheel hash is the same pin
    # formerly held by uv.lock; moving it here removes the direct-URL package
    # metadata that PyPI rejects while retaining exact reproducibility.
    "spacy:en_core_web_sm@3.8.0": PinnedArtifact(
        ref="spacy:en_core_web_sm@3.8.0",
        kind="spacy_model",
        display_name="spaCy English pipeline (en_core_web_sm)",
        manifest_version="2026.08.1",
        license="MIT",
        license_url="https://github.com/explosion/spacy-models/blob/master/LICENSE",
        source_url=(
            "https://github.com/explosion/spacy-models/releases/tag/"
            "en_core_web_sm-3.8.0"
        ),
        files=(
            PinnedFile(
                "en_core_web_sm-3.8.0-py3-none-any.whl",
                "https://github.com/explosion/spacy-models/releases/download/"
                "en_core_web_sm-3.8.0/"
                "en_core_web_sm-3.8.0-py3-none-any.whl",
                "1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85",
                12_806_118,
            ),
        ),
    ),
    "opus-mt:en-es": PinnedArtifact(
        ref="opus-mt:en-es",
        kind="ct2_pair",
        display_name="English → Spanish",
        manifest_version="2026.07.1",
        license="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0",
        source_url="https://huggingface.co/frisket-models/opus-mt-en-es-ct2/tree/010363dc108c7a1ddf2a57880f1592b0584bce8c",
        files=(
            PinnedFile(
                "config.json",
                "https://huggingface.co/frisket-models/opus-mt-en-es-ct2/resolve/010363dc108c7a1ddf2a57880f1592b0584bce8c/config.json",
                "8f6496adfc930cbfecbe8281112197705c488fab47d34b4829b06d7f478909af",
                223,
            ),
            PinnedFile(
                "model.bin",
                "https://huggingface.co/frisket-models/opus-mt-en-es-ct2/resolve/010363dc108c7a1ddf2a57880f1592b0584bce8c/model.bin",
                "30c5c2de08329c61860777fbe471e2dd413f64adbde543b2848b5ac3b5d6f865",
                79567635,
            ),
            PinnedFile(
                "shared_vocabulary.json",
                "https://huggingface.co/frisket-models/opus-mt-en-es-ct2/resolve/010363dc108c7a1ddf2a57880f1592b0584bce8c/shared_vocabulary.json",
                "040e48c9d00734f48506e052695837720bd1f6aa01c70636cde83038362baf19",
                1276136,
            ),
            PinnedFile(
                "source.spm",
                "https://huggingface.co/frisket-models/opus-mt-en-es-ct2/resolve/010363dc108c7a1ddf2a57880f1592b0584bce8c/source.spm",
                "4dd547c24816a335e7b0b2e63376a8f1b3cbfc671eda5ab808dd44fdadaa8791",
                801636,
            ),
            PinnedFile(
                "target.spm",
                "https://huggingface.co/frisket-models/opus-mt-en-es-ct2/resolve/010363dc108c7a1ddf2a57880f1592b0584bce8c/target.spm",
                "e236ee6d866b635c0142114f8647f39831f9d92534aa2aad75c942f6a78ad0e3",
                825924,
            ),
        ),
    ),
    "opus-mt:es-en": PinnedArtifact(
        ref="opus-mt:es-en",
        kind="ct2_pair",
        display_name="Spanish → English",
        manifest_version="2026.07.1",
        license="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0",
        source_url="https://huggingface.co/frisket-models/opus-mt-es-en-ct2/tree/21fadee65fdf7c7ab92e49a9deeee20b202a9855",
        files=(
            PinnedFile(
                "config.json",
                "https://huggingface.co/frisket-models/opus-mt-es-en-ct2/resolve/21fadee65fdf7c7ab92e49a9deeee20b202a9855/config.json",
                "8f6496adfc930cbfecbe8281112197705c488fab47d34b4829b06d7f478909af",
                223,
            ),
            PinnedFile(
                "model.bin",
                "https://huggingface.co/frisket-models/opus-mt-es-en-ct2/resolve/21fadee65fdf7c7ab92e49a9deeee20b202a9855/model.bin",
                "44c5adc2c680f27c14c991e5ab7f74f38b41597153f7123bc8f6455f09a3b38b",
                79567635,
            ),
            PinnedFile(
                "shared_vocabulary.json",
                "https://huggingface.co/frisket-models/opus-mt-es-en-ct2/resolve/21fadee65fdf7c7ab92e49a9deeee20b202a9855/shared_vocabulary.json",
                "040e48c9d00734f48506e052695837720bd1f6aa01c70636cde83038362baf19",
                1276136,
            ),
            PinnedFile(
                "source.spm",
                "https://huggingface.co/frisket-models/opus-mt-es-en-ct2/resolve/21fadee65fdf7c7ab92e49a9deeee20b202a9855/source.spm",
                "e236ee6d866b635c0142114f8647f39831f9d92534aa2aad75c942f6a78ad0e3",
                825924,
            ),
            PinnedFile(
                "target.spm",
                "https://huggingface.co/frisket-models/opus-mt-es-en-ct2/resolve/21fadee65fdf7c7ab92e49a9deeee20b202a9855/target.spm",
                "4dd547c24816a335e7b0b2e63376a8f1b3cbfc671eda5ab808dd44fdadaa8791",
                801636,
            ),
        ),
    ),
    "opus-mt:en-fr": PinnedArtifact(
        ref="opus-mt:en-fr",
        kind="ct2_pair",
        display_name="English → French",
        manifest_version="2026.07.1",
        license="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0",
        source_url="https://huggingface.co/frisket-models/opus-mt-en-fr-ct2/tree/01fd244b378d48480d529abc986452f9682aa064",
        files=(
            PinnedFile(
                "config.json",
                "https://huggingface.co/frisket-models/opus-mt-en-fr-ct2/resolve/01fd244b378d48480d529abc986452f9682aa064/config.json",
                "8f6496adfc930cbfecbe8281112197705c488fab47d34b4829b06d7f478909af",
                223,
            ),
            PinnedFile(
                "model.bin",
                "https://huggingface.co/frisket-models/opus-mt-en-fr-ct2/resolve/01fd244b378d48480d529abc986452f9682aa064/model.bin",
                "236b63e3029611a328693d704067ebdd75b800e5b3011cdec3dad4287fd29a36",
                76714395,
            ),
            PinnedFile(
                "shared_vocabulary.json",
                "https://huggingface.co/frisket-models/opus-mt-en-fr-ct2/resolve/01fd244b378d48480d529abc986452f9682aa064/shared_vocabulary.json",
                "a157eb51a42817dd1ae4bc46cecf66e591ab32641df936da292bd09f2728631b",
                1052697,
            ),
            PinnedFile(
                "source.spm",
                "https://huggingface.co/frisket-models/opus-mt-en-fr-ct2/resolve/01fd244b378d48480d529abc986452f9682aa064/source.spm",
                "173e9f493a668fe396d599e28d414a201193094e6ffd7a4678e5aab0f6d3d838",
                778395,
            ),
            PinnedFile(
                "target.spm",
                "https://huggingface.co/frisket-models/opus-mt-en-fr-ct2/resolve/01fd244b378d48480d529abc986452f9682aa064/target.spm",
                "78d0e717c77053f1c4b856d8661d9cb87c64f083a35418c087b9146300e4f585",
                802397,
            ),
        ),
    ),
    "opus-mt:fr-en": PinnedArtifact(
        ref="opus-mt:fr-en",
        kind="ct2_pair",
        display_name="French → English",
        manifest_version="2026.07.1",
        license="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0",
        source_url="https://huggingface.co/frisket-models/opus-mt-fr-en-ct2/tree/46c82a1311938f74a08085264763ece40da4f72c",
        files=(
            PinnedFile(
                "config.json",
                "https://huggingface.co/frisket-models/opus-mt-fr-en-ct2/resolve/46c82a1311938f74a08085264763ece40da4f72c/config.json",
                "8f6496adfc930cbfecbe8281112197705c488fab47d34b4829b06d7f478909af",
                223,
            ),
            PinnedFile(
                "model.bin",
                "https://huggingface.co/frisket-models/opus-mt-fr-en-ct2/resolve/46c82a1311938f74a08085264763ece40da4f72c/model.bin",
                "8c070c110a94dca4693be7c93ecbfdd09bce99eefbf902b1e7b45cbea2a10fd4",
                76714395,
            ),
            PinnedFile(
                "shared_vocabulary.json",
                "https://huggingface.co/frisket-models/opus-mt-fr-en-ct2/resolve/46c82a1311938f74a08085264763ece40da4f72c/shared_vocabulary.json",
                "a157eb51a42817dd1ae4bc46cecf66e591ab32641df936da292bd09f2728631b",
                1052697,
            ),
            PinnedFile(
                "source.spm",
                "https://huggingface.co/frisket-models/opus-mt-fr-en-ct2/resolve/46c82a1311938f74a08085264763ece40da4f72c/source.spm",
                "78d0e717c77053f1c4b856d8661d9cb87c64f083a35418c087b9146300e4f585",
                802397,
            ),
            PinnedFile(
                "target.spm",
                "https://huggingface.co/frisket-models/opus-mt-fr-en-ct2/resolve/46c82a1311938f74a08085264763ece40da4f72c/target.spm",
                "173e9f493a668fe396d599e28d414a201193094e6ffd7a4678e5aab0f6d3d838",
                778395,
            ),
        ),
    ),
    "opus-mt:en-de": PinnedArtifact(
        ref="opus-mt:en-de",
        kind="ct2_pair",
        display_name="English → German",
        manifest_version="2026.07.1",
        license="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        source_url="https://huggingface.co/frisket-models/opus-mt-en-de-ct2/tree/670414c0e4c7a87b896f2f5d7e9dbd149ebb748b",
        files=(
            PinnedFile(
                "config.json",
                "https://huggingface.co/frisket-models/opus-mt-en-de-ct2/resolve/670414c0e4c7a87b896f2f5d7e9dbd149ebb748b/config.json",
                "8f6496adfc930cbfecbe8281112197705c488fab47d34b4829b06d7f478909af",
                223,
            ),
            PinnedFile(
                "model.bin",
                "https://huggingface.co/frisket-models/opus-mt-en-de-ct2/resolve/670414c0e4c7a87b896f2f5d7e9dbd149ebb748b/model.bin",
                "3cad348e65aa400a0fce83b2b89a876030092e108ebc5a824ac1def6c76957b2",
                75979635,
            ),
            PinnedFile(
                "shared_vocabulary.json",
                "https://huggingface.co/frisket-models/opus-mt-en-de-ct2/resolve/670414c0e4c7a87b896f2f5d7e9dbd149ebb748b/shared_vocabulary.json",
                "fa4c8d666c582ad39892a2ae5d4ded1b0ddde4b49dc784a7fcbd73940f9fa13e",
                993828,
            ),
            PinnedFile(
                "source.spm",
                "https://huggingface.co/frisket-models/opus-mt-en-de-ct2/resolve/670414c0e4c7a87b896f2f5d7e9dbd149ebb748b/source.spm",
                "678f2a1177d8389f67b66299762dcc4fc567e89b07e212ba91b0c56daecf47ce",
                768489,
            ),
            PinnedFile(
                "target.spm",
                "https://huggingface.co/frisket-models/opus-mt-en-de-ct2/resolve/670414c0e4c7a87b896f2f5d7e9dbd149ebb748b/target.spm",
                "bbd1f495eea99c8e21ae086d9146e0fa7b096c3dfdd9ba07ab8b631889df5c9b",
                796845,
            ),
        ),
    ),
    "opus-mt:de-en": PinnedArtifact(
        ref="opus-mt:de-en",
        kind="ct2_pair",
        display_name="German → English",
        manifest_version="2026.07.1",
        license="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0",
        source_url="https://huggingface.co/frisket-models/opus-mt-de-en-ct2/tree/5aa6c0660768e7e8c656814eb5dbe7a1b1f482c5",
        files=(
            PinnedFile(
                "config.json",
                "https://huggingface.co/frisket-models/opus-mt-de-en-ct2/resolve/5aa6c0660768e7e8c656814eb5dbe7a1b1f482c5/config.json",
                "8f6496adfc930cbfecbe8281112197705c488fab47d34b4829b06d7f478909af",
                223,
            ),
            PinnedFile(
                "model.bin",
                "https://huggingface.co/frisket-models/opus-mt-de-en-ct2/resolve/5aa6c0660768e7e8c656814eb5dbe7a1b1f482c5/model.bin",
                "399ce16d36b721cd166fe973b31c1f1195b92af200665eccd9a7fc62263e02ec",
                75979635,
            ),
            PinnedFile(
                "shared_vocabulary.json",
                "https://huggingface.co/frisket-models/opus-mt-de-en-ct2/resolve/5aa6c0660768e7e8c656814eb5dbe7a1b1f482c5/shared_vocabulary.json",
                "fa4c8d666c582ad39892a2ae5d4ded1b0ddde4b49dc784a7fcbd73940f9fa13e",
                993828,
            ),
            PinnedFile(
                "source.spm",
                "https://huggingface.co/frisket-models/opus-mt-de-en-ct2/resolve/5aa6c0660768e7e8c656814eb5dbe7a1b1f482c5/source.spm",
                "bbd1f495eea99c8e21ae086d9146e0fa7b096c3dfdd9ba07ab8b631889df5c9b",
                796845,
            ),
            PinnedFile(
                "target.spm",
                "https://huggingface.co/frisket-models/opus-mt-de-en-ct2/resolve/5aa6c0660768e7e8c656814eb5dbe7a1b1f482c5/target.spm",
                "678f2a1177d8389f67b66299762dcc4fc567e89b07e212ba91b0c56daecf47ce",
                768489,
            ),
        ),
    ),
    # Hy-MT2 1.8B experimental GGUF tier. Single-file hf artifact, llama.cpp
    # runtime behind the `translate-gguf` extra. Quantization: Q4_K_M
    # (~1.13GB) -- the pragmatic default: materially smaller than Q6_K
    # (1.48GB) / Q8_0 (1.91GB) with negligible quality loss for a 1.8B MT model,
    # and the size where the resume-on-interrupt path earns its keep. The
    # SHA256 was computed from one authorized live download of this exact
    # revision. Apache-2.0 (plain license, verified on the model card).
    "hf:tencent/Hy-MT2-1.8B-GGUF@1cd5208700acedef4ef93019b6cfc148b8522d45/Hy-MT2-1.8B-Q4_K_M.gguf": PinnedArtifact(
        ref="hf:tencent/Hy-MT2-1.8B-GGUF@1cd5208700acedef4ef93019b6cfc148b8522d45/Hy-MT2-1.8B-Q4_K_M.gguf",
        kind="hf_file",
        display_name="Hy-MT2 1.8B (Q4_K_M)",
        manifest_version="2026.07.1",
        license="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0",
        source_url="https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF/tree/1cd5208700acedef4ef93019b6cfc148b8522d45",
        files=(
            PinnedFile(
                "Hy-MT2-1.8B-Q4_K_M.gguf",
                "https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF/resolve/1cd5208700acedef4ef93019b6cfc148b8522d45/Hy-MT2-1.8B-Q4_K_M.gguf",
                "dc5f44fcf1fa496ee7ad725982c0c8c553a4de00259b53af84c4b89fb0c06699",
                1133080448,
            ),
        ),
    ),
    # Parakeet ASR (revision-pinned, not checksummed -- see the module
    # docstring's "hf_revision_pinned" guarantee). repo id lives above
    # (PARAKEET_MODEL_HF_REPO); revision + file list are imported from
    # `_workers/parakeet_model.py`, the shared zero-import leaf, so this pin
    # and the runtime binding can never drift apart. License: CC-BY-4.0,
    # matching the upstream NVIDIA NeMo Parakeet checkpoint this ONNX export
    # is converted from (needs a maintainer re-check against the exact HF
    # model card at pin time, same as any other entry here).
    f"hf-snapshot:{PARAKEET_MODEL_HF_REPO}@{_PARAKEET_MODEL_REVISION}": PinnedArtifact(
        ref=f"hf-snapshot:{PARAKEET_MODEL_HF_REPO}@{_PARAKEET_MODEL_REVISION}",
        kind="hf_snapshot",
        display_name="Parakeet TDT 0.6B v3 (ONNX, int8)",
        manifest_version="2026.09.1",
        license="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        source_url=f"https://huggingface.co/{PARAKEET_MODEL_HF_REPO}/tree/{_PARAKEET_MODEL_REVISION}",
        files=(),
        hf_snapshot=HfSnapshotSource(
            repo_id=PARAKEET_MODEL_HF_REPO,
            revision=_PARAKEET_MODEL_REVISION,
            files=_PARAKEET_MODEL_FILES,
        ),
        # The selected four-file int8 ASR snapshot is ~670 MB. VAD is
        # catalogued as a separate artifact below.
        approx_size_bytes=670_480_039,
    ),
    # Silero VAD, the optional voice-activity companion for Parakeet. Same
    # revision-pinned guarantee as the model entry above. License: MIT,
    # matching upstream Silero VAD (also needs the same maintainer re-check).
    f"hf-snapshot:{PARAKEET_VAD_HF_REPO}@{_PARAKEET_VAD_REVISION}": PinnedArtifact(
        ref=f"hf-snapshot:{PARAKEET_VAD_HF_REPO}@{_PARAKEET_VAD_REVISION}",
        kind="hf_snapshot",
        display_name="Silero VAD (ONNX)",
        manifest_version="2026.07.2",
        license="MIT",
        license_url="https://opensource.org/licenses/MIT",
        source_url=f"https://huggingface.co/{PARAKEET_VAD_HF_REPO}/tree/{_PARAKEET_VAD_REVISION}",
        files=(),
        hf_snapshot=HfSnapshotSource(
            repo_id=PARAKEET_VAD_HF_REPO,
            revision=_PARAKEET_VAD_REVISION,
            files=_PARAKEET_VAD_FILES,
        ),
        approx_size_bytes=2_000_000,
    ),
    # faster-whisper 'base'. Revision + file list captured live via
    # `HfApi.model_info(repo, files_metadata=True)` (no weight bytes fetched
    # -- metadata only); license MIT, matching the model card (Systran's
    # CTranslate2 conversion of OpenAI's MIT-licensed Whisper `base`). Byte
    # total below is the exact sum of the four pinned files' reported sizes
    # at that same revision (config.json 2_309 + model.bin 145_217_532 +
    # tokenizer.json 2_203_239 + vocabulary.txt 459_861 = 147_882_941, ~145
    # MB) -- like Parakeet's entries, this is a UI-disclosure number only,
    # not an integrity input.
    f"hf-snapshot:{WHISPER_BASE_HF_REPO}@ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66": PinnedArtifact(
        ref=f"hf-snapshot:{WHISPER_BASE_HF_REPO}@ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
        kind="hf_snapshot",
        display_name="Whisper base (faster-whisper, CTranslate2)",
        manifest_version="2026.07.3",
        license="MIT",
        license_url="https://opensource.org/licenses/MIT",
        source_url=f"https://huggingface.co/{WHISPER_BASE_HF_REPO}/tree/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
        files=(),
        hf_snapshot=HfSnapshotSource(
            repo_id=WHISPER_BASE_HF_REPO,
            revision="ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
            files=("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"),
        ),
        approx_size_bytes=147_882_941,
    ),
    f"hf-snapshot:{_PROVIDERLESS_CLASSIFY_HF_REPO}@{_PROVIDERLESS_CLASSIFY_HF_REVISION}": PinnedArtifact(
        ref=f"hf-snapshot:{_PROVIDERLESS_CLASSIFY_HF_REPO}@{_PROVIDERLESS_CLASSIFY_HF_REVISION}",
        kind="hf_snapshot",
        display_name="BGE small English v1.5 (FastEmbed, ONNX)",
        manifest_version="2026.08.2",
        license="Apache-2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0",
        source_url=f"https://huggingface.co/{_PROVIDERLESS_CLASSIFY_HF_REPO}/tree/{_PROVIDERLESS_CLASSIFY_HF_REVISION}",
        files=(),
        hf_snapshot=HfSnapshotSource(
            repo_id=_PROVIDERLESS_CLASSIFY_HF_REPO,
            revision=_PROVIDERLESS_CLASSIFY_HF_REVISION,
            files=_PROVIDERLESS_CLASSIFY_HF_FILES,
        ),
    ),
}


# The pinned Hy-MT2 GGUF artifact ref. A module constant so the runtime
# binding and the catalog reference ONE pin; re-quantizing or re-pinning
# to a new revision updates it in exactly this one place.
HY_MT2_REF = (
    "hf:tencent/Hy-MT2-1.8B-GGUF@1cd5208700acedef4ef93019b6cfc148b8522d45"
    "/Hy-MT2-1.8B-Q4_K_M.gguf"
)

# The pinned Parakeet model/VAD refs. Module constants so the runtime binding
# (`_workers/parakeet_artifacts.py`) and this catalog reference ONE pin each.
PARAKEET_MODEL_REF = f"hf-snapshot:{PARAKEET_MODEL_HF_REPO}@{_PARAKEET_MODEL_REVISION}"
PARAKEET_VAD_REF = f"hf-snapshot:{PARAKEET_VAD_HF_REPO}@{_PARAKEET_VAD_REVISION}"
PROVIDERLESS_CLASSIFY_REF = (
    f"hf-snapshot:{_PROVIDERLESS_CLASSIFY_HF_REPO}@{_PROVIDERLESS_CLASSIFY_HF_REVISION}"
)

# The pinned faster-whisper 'base' ref. A module constant so the runtime
# binding (`_workers/faster_whisper_worker.py`) and this catalog reference
# ONE pin, same as the two Parakeet refs above.
WHISPER_BASE_REF = (
    f"hf-snapshot:{WHISPER_BASE_HF_REPO}@ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"
)

# The runtime-managed spaCy wheel. One constant binds the grammar, loader,
# doctor report, catalog download prompt, and manifest to the same exact pin.
SPACY_MODEL_REF = "spacy:en_core_web_sm@3.8.0"


def lookup(ref: str) -> PinnedArtifact | None:
    """Return the pinned entry for a canonical ref, or None if unpinned."""
    return _MANIFEST.get(ref)


def hy_mt2_artifact() -> PinnedArtifact | None:
    """The pinned Hy-MT2 GGUF entry, or None if unpinned."""
    return _MANIFEST.get(HY_MT2_REF)


def parakeet_model_artifact() -> PinnedArtifact | None:
    """The pinned Parakeet ASR model entry, or None if unpinned."""
    return _MANIFEST.get(PARAKEET_MODEL_REF)


def parakeet_vad_artifact() -> PinnedArtifact | None:
    """The pinned Silero VAD entry, or None if unpinned."""
    return _MANIFEST.get(PARAKEET_VAD_REF)


def providerless_classify_artifact() -> PinnedArtifact | None:
    """The pinned providerless classifier snapshot, or None if unpinned."""
    return _MANIFEST.get(PROVIDERLESS_CLASSIFY_REF)


def whisper_base_artifact() -> PinnedArtifact | None:
    """The pinned faster-whisper 'base' entry, or None if unpinned."""
    return _MANIFEST.get(WHISPER_BASE_REF)


def spacy_model_artifact() -> PinnedArtifact | None:
    """The pinned en_core_web_sm wheel, or None if the manifest is broken."""
    return _MANIFEST.get(SPACY_MODEL_REF)


def all_pinned() -> tuple[PinnedArtifact, ...]:
    """Every pinned entry (for the manage-models surface + catalog tests)."""
    return tuple(_MANIFEST.values())


def installed_opus_pairs_available() -> tuple[str, ...]:
    """Canonical pair ids (``en-es``) of every pinned opus-mt entry -- the
    downloadable roster the translate catalog offers before install."""
    out: list[str] = []
    for entry in _MANIFEST.values():
        if entry.kind == "ct2_pair" and entry.ref.startswith("opus-mt:"):
            out.append(entry.ref[len("opus-mt:") :])
    return tuple(sorted(out))


__all__ = [
    "FRISKET_HF_ORG",
    "HY_MT2_REF",
    "PARAKEET_MODEL_HF_REPO",
    "PARAKEET_MODEL_REF",
    "PARAKEET_VAD_HF_REPO",
    "PARAKEET_VAD_REF",
    "PROVIDERLESS_CLASSIFY_REF",
    "SPACY_MODEL_REF",
    "WHISPER_BASE_HF_REPO",
    "WHISPER_BASE_REF",
    "HfSnapshotSource",
    "PinnedArtifact",
    "PinnedFile",
    "all_pinned",
    "hy_mt2_artifact",
    "installed_opus_pairs_available",
    "lookup",
    "opus_mt_source_url",
    "parakeet_model_artifact",
    "parakeet_vad_artifact",
    "providerless_classify_artifact",
    "whisper_base_artifact",
    "spacy_model_artifact",
]
