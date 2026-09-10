"""Scheme-prefixed artifact reference grammar.

The generic handler pulls local-server models plus HF / CTranslate2 / GGUF /
spaCy artifacts, so a ref must name each source without the handler sniffing:

- ``ollama/@<endpoint-id>/<model>`` -- a strict endpoint-qualified local model
  reference. The endpoint identity remains in the canonical stored value so
  pulls, retries, deduplication, and provenance cannot silently switch servers.
- ``opus-mt:`` -- a logical translate-pair id, ``opus-mt:<src>-<tgt>`` where
  each side is a lowercase ISO-639 subtag. The availability unit for the local
  Opus-MT translate engine. Canonical form keeps the prefix.
- ``hf:`` -- a raw Hugging Face file artifact,
  ``hf:<owner>/<repo>@<revision>/<path/to/file>``. The revision is MANDATORY
  (never resolve ``main`` at pull time -- that is an unpinnable supply-chain
  hop). Canonical form keeps the prefix.
- ``hf-snapshot:`` -- a revision-pinned multi-file HF snapshot,
  ``hf-snapshot:<owner>/<repo>@<revision>``. Canonical form keeps the prefix.
  Unlike every other scheme, this one IS manifest-gated at parse time: only
  refs present in the pinned manifest (``artifact_manifest``) parse at all.
  The adversary is a request holder using the pull route to make this server
  download arbitrary repos' weights (a multi-file snapshot has a bigger blast
  radius than a single unpinned ``hf:`` file, so the free-form carve-out does
  not extend to it); the maintainer-reviewed manifest is that boundary, and
  past it there is nothing to defend -- a pinned entry is already vetted.
- ``spacy:`` -- a manifest-gated spaCy pipeline wheel,
  ``spacy:<package>@<version>``. There is no free-form spelling: Frisket only
  downloads the exact reviewed wheel pin carried in its in-tree manifest.

Charset/injection discipline carries the Ollama grammar's posture forward as
hard allowlists, never sniffing: pair subtags are allow-listed, the
``hf:`` repo id / revision / path are each allow-listed, ``..`` traversal and
absolute paths are rejected, and the file extension must be on a small
allowlist.

Manifest-gating is NOT enforced here for local models, ``opus-mt:``, or ``hf:``.
Free-form ``hf:`` and on-demand ``opus-mt:`` pulls are allowed behind an
"unpinned -- no checksum, no license vetting, on you"
acknowledgment -- for those schemes this module validates *shape* only and
the puller performs the manifest lookup (``artifact_manifest.lookup``).
``hf-snapshot:`` is the exception: the manifest is its allowlist, enforced
right here (see above), so an unpinned snapshot ref never becomes a valid
``ArtifactRef`` in the first place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from frisket.engine.jobs.model_pull import InvalidModelRefError, normalize_model_ref
from frisket.local_model_ids import parse_local_model_id

# The hf: form (owner/repo@rev/path) is longer than the 160-char Ollama cap.
MAX_ARTIFACT_REF_LENGTH = 320

_OLLAMA_SCHEME = "ollama"
_OPUS_MT_SCHEME = "opus-mt"
_HF_SCHEME = "hf"
_HF_SNAPSHOT_SCHEME = "hf-snapshot"
_SPACY_SCHEME = "spacy"

# opus-mt pair sides: a lowercase ISO-639 primary subtag, optionally one
# BCP-47 extension subtag (e.g. ``zh``, ``pt``, ``zh-hans``).
_PAIR_SIDE_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{1,8})?$")

# hf: repo id -- ``owner/name``. Dots ARE allowed (an HF repo id is not a
# registry host, unlike the Ollama namespace which forbids dots precisely to
# reject a registry). Each component is conservative and length-capped.
_HF_REPO_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"
)
# hf: revision -- a git SHA or a tag.
_HF_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# hf: file-path segment -- the Ollama component charset (no dot-leading tricks
# beyond the allowlist; ``..`` is rejected explicitly below).
_HF_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Only these artifact file types may be pulled.
_HF_ALLOWED_EXTENSIONS = (
    ".gguf",
    ".bin",
    ".onnx",
    ".safetensors",
    ".json",
    ".spm",
    ".model",
    ".txt",
)
_SPACY_MODEL_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}@[0-9]+(?:\.[0-9]+){1,3}$")


@dataclass(frozen=True)
class ArtifactRef:
    """A validated, canonicalized artifact reference.

    ``canonical`` is the string stored in ``model_pulls.model_ref`` and shown
    in the DTO's ``model`` field. Every scheme keeps its prefix; local-server
    models also keep the endpoint ID.
    """

    scheme: str
    canonical: str
    ollama_ref: str | None = None
    endpoint_id: str | None = None
    pair: str | None = None
    hf_repo: str | None = None
    hf_revision: str | None = None
    hf_path: str | None = None
    spacy_model: str | None = None
    spacy_version: str | None = None

    @property
    def is_ollama(self) -> bool:
        return self.scheme == _OLLAMA_SCHEME


def _split_scheme(text: str) -> tuple[str | None, str]:
    """Split a leading ``<scheme>:`` prefix, but ONLY for known schemes. An
    artifact identifier may itself contain ``:`` (for example a model tag),
    so only an exact known scheme token is peeled."""
    for scheme in (
        _OPUS_MT_SCHEME,
        _HF_SNAPSHOT_SCHEME,
        _SPACY_SCHEME,
        _HF_SCHEME,
    ):
        prefix = f"{scheme}:"
        if text.startswith(prefix):
            return scheme, text[len(prefix) :]
    return None, text


def _normalize_opus_mt(identifier: str) -> ArtifactRef:
    # A pair side may itself contain a ``-`` (``zh-hans``), so we cannot simply
    # split on ``-``. The pair is ``<src>-<tgt>``; try every split point and
    # accept the first where BOTH sides match the side grammar.
    if "-" not in identifier:
        raise InvalidModelRefError(
            "opus-mt reference must be '<src>-<tgt>' (e.g. opus-mt:en-es)"
        )
    parts = identifier.split("-")
    src = tgt = None
    for i in range(1, len(parts)):
        left = "-".join(parts[:i])
        right = "-".join(parts[i:])
        if _PAIR_SIDE_RE.match(left) and _PAIR_SIDE_RE.match(right):
            src, tgt = left, right
            break
    if src is None or tgt is None:
        raise InvalidModelRefError(
            "opus-mt language pair has an invalid format "
            "(each side must be a lowercase ISO-639 subtag)"
        )
    pair = f"{src}-{tgt}"
    return ArtifactRef(
        scheme=_OPUS_MT_SCHEME,
        canonical=f"{_OPUS_MT_SCHEME}:{pair}",
        pair=pair,
    )


def _normalize_hf(identifier: str) -> ArtifactRef:
    # Shape: ``<owner>/<repo>@<revision>/<path/to/file>``.
    if "@" not in identifier:
        raise InvalidModelRefError(
            "hf reference must pin a revision: 'owner/repo@<revision>/path' "
            "(never an unpinned 'main')"
        )
    repo_part, rev_and_path = identifier.split("@", 1)
    if not _HF_REPO_RE.match(repo_part):
        raise InvalidModelRefError("hf repository id has an invalid format")
    if "/" not in rev_and_path:
        raise InvalidModelRefError(
            "hf reference must name a file after the revision: "
            "'owner/repo@<revision>/path'"
        )
    revision, path = rev_and_path.split("/", 1)
    if not _HF_REVISION_RE.match(revision):
        raise InvalidModelRefError("hf revision has an invalid format")
    if not path or path.startswith("/"):
        raise InvalidModelRefError("hf file path must be repo-relative")
    segments = path.split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise InvalidModelRefError(
                "hf file path may not contain empty or '..' segments"
            )
        if not _HF_PATH_SEGMENT_RE.match(segment):
            raise InvalidModelRefError("hf file path segment has an invalid format")
    lower = path.lower()
    if not any(lower.endswith(ext) for ext in _HF_ALLOWED_EXTENSIONS):
        raise InvalidModelRefError(
            "hf file extension is not in the allowlist "
            f"({', '.join(_HF_ALLOWED_EXTENSIONS)})"
        )
    canonical = f"{_HF_SCHEME}:{repo_part}@{revision}/{path}"
    return ArtifactRef(
        scheme=_HF_SCHEME,
        canonical=canonical,
        hf_repo=repo_part,
        hf_revision=revision,
        hf_path=path,
    )


def _normalize_hf_snapshot(identifier: str) -> ArtifactRef:
    # Shape: ``<owner>/<repo>@<revision>`` -- no file path (the pinned file
    # list lives in the manifest entry, not the ref).
    if "@" not in identifier:
        raise InvalidModelRefError(
            "hf-snapshot reference must pin a revision: 'owner/repo@<revision>'"
        )
    repo_part, revision = identifier.split("@", 1)
    if not _HF_REPO_RE.match(repo_part):
        raise InvalidModelRefError("hf-snapshot repository id has an invalid format")
    if not _HF_REVISION_RE.match(revision):
        raise InvalidModelRefError("hf-snapshot revision has an invalid format")
    canonical = f"{_HF_SNAPSHOT_SCHEME}:{repo_part}@{revision}"
    # The manifest is the allowlist (module docstring: the boundary against a
    # request pulling arbitrary weights). Local import keeps this jobs-package
    # leaf import-light for every non-snapshot caller.
    from frisket.ai.models import artifact_manifest

    pinned = artifact_manifest.lookup(canonical)
    if pinned is None or pinned.kind != "hf_snapshot":
        raise InvalidModelRefError(
            "this snapshot is not in the pinned catalog -- only pinned "
            "artifacts can be pulled as hf-snapshot references"
        )
    return ArtifactRef(
        scheme=_HF_SNAPSHOT_SCHEME,
        canonical=canonical,
        hf_repo=repo_part,
        hf_revision=revision,
    )


def _normalize_spacy(identifier: str) -> ArtifactRef:
    if not _SPACY_MODEL_RE.fullmatch(identifier):
        raise InvalidModelRefError(
            "spacy reference must be '<package>@<version>' "
            "(for example spacy:en_core_web_sm@3.8.0)"
        )
    model, version = identifier.rsplit("@", 1)
    canonical = f"{_SPACY_SCHEME}:{model}@{version}"
    # spaCy wheels are executable Python distributions fetched from GitHub.
    # The reviewed in-tree manifest is therefore the allowlist; unlike hf:
    # there is deliberately no free-form/unpinned escape hatch.
    from frisket.ai.models import artifact_manifest

    pinned = artifact_manifest.lookup(canonical)
    if pinned is None or pinned.kind != "spacy_model":
        raise InvalidModelRefError(
            "this spaCy model is not in the pinned catalog -- only pinned "
            "spaCy artifacts can be downloaded"
        )
    return ArtifactRef(
        scheme=_SPACY_SCHEME,
        canonical=canonical,
        spacy_model=model,
        spacy_version=version,
    )


def normalize_artifact_ref(raw: str) -> ArtifactRef:
    """Validate + canonicalize a scheme-prefixed artifact reference.

    Delegates the model-name tail of a qualified ``ollama`` reference to
    ``normalize_model_ref`` after the shared endpoint parser validates identity.
    Raises :class:`~frisket.jobs.model_pull.InvalidModelRefError` on any
    violation, so existing route error handling (``invalid_model_ref``,
    HTTP 400) is unchanged.
    """
    text = (raw or "").strip()
    if not text:
        raise InvalidModelRefError("artifact reference is required")
    if len(text) > MAX_ARTIFACT_REF_LENGTH:
        raise InvalidModelRefError(
            f"artifact reference is too long (max {MAX_ARTIFACT_REF_LENGTH} characters)"
        )

    if text.startswith("ollama/"):
        try:
            endpoint_id, bare_model = parse_local_model_id(text)
        except ValueError as exc:
            raise InvalidModelRefError(str(exc)) from exc
        ollama_ref = normalize_model_ref(bare_model)
        canonical = f"ollama/@{endpoint_id}/{ollama_ref}"
        return ArtifactRef(
            scheme=_OLLAMA_SCHEME,
            canonical=canonical,
            ollama_ref=ollama_ref,
            endpoint_id=endpoint_id,
        )
    scheme, identifier = _split_scheme(text)
    if scheme is None:
        raise InvalidModelRefError(
            "local model artifact reference must use ollama/@<endpoint-id>/<model>"
        )
    if scheme == _OPUS_MT_SCHEME:
        return _normalize_opus_mt(identifier)
    if scheme == _HF_SNAPSHOT_SCHEME:
        return _normalize_hf_snapshot(identifier)
    if scheme == _SPACY_SCHEME:
        return _normalize_spacy(identifier)
    if scheme == _HF_SCHEME:
        return _normalize_hf(identifier)
    # _split_scheme only returns a known scheme or None.
    raise InvalidModelRefError("unknown artifact reference scheme")


__all__ = [
    "MAX_ARTIFACT_REF_LENGTH",
    "ArtifactRef",
    "InvalidModelRefError",
    "normalize_artifact_ref",
]
