"""normalize_model_ref grammar.

Registry-less only: ``name[:tag]`` or ``namespace/name[:tag]``, conservative
charset, length-capped, no registry host. A registry-qualified ref
(``evil.example/x/y``) is a second-hop egress primitive -- the model host
would connect to an attacker-chosen registry -- so it is rejected outright,
never passed through as a payload value.
"""

from __future__ import annotations

import pytest

from frisket.engine.jobs.model_pull import (
    MAX_MODEL_REF_LENGTH,
    InvalidModelRefError,
    normalize_model_ref,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("smollm", "smollm:latest"),
        ("smollm:135m", "smollm:135m"),
        ("library/smollm:135m", "library/smollm:135m"),
        ("qwen3:8b", "qwen3:8b"),
        # canonicalization: lowercase name/namespace, tag case preserved
        ("SmollM", "smollm:latest"),
        ("SMOLLM:Latest-Tag", "smollm:Latest-Tag"),
        ("Library/SmollM:V1.0", "library/smollm:V1.0"),
        # dots/underscores/hyphens are valid component characters
        ("my-model_v2.1:tag_1.2", "my-model_v2.1:tag_1.2"),
    ],
)
def test_accepts_and_canonicalizes(raw: str, expected: str) -> None:
    assert normalize_model_ref(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        # registry-qualified: a namespace containing a dot is always a
        # registry host (docker.io, ghcr.io, evil.example, ...)
        "evil.example/x",
        "evil.example/x/y",
        "docker.io/library/smollm",
        "host.with.dots/x:tag",
        # more than one namespace/name separator
        "a/b/c",
        "a/b/c:tag",
        # a host:port-style prefix is not a valid tag
        "registry.example.com:5000/name",
        # invalid characters
        "name with spaces",
        "name/../etc/passwd",
        "..",
        "../x",
        "name:",
        ":tag",
        "name:tag with spaces",
        "name:" + "a" * 65,  # tag over 64 chars
        "_leadingunderscore",
        "-leadinghyphen",
        ".leadingdot",
        # path traversal junk
        "../../../etc/passwd",
    ],
)
def test_rejects_invalid_refs(raw: str) -> None:
    with pytest.raises(InvalidModelRefError):
        normalize_model_ref(raw)


def test_rejects_over_length_cap() -> None:
    too_long = "a" * (MAX_MODEL_REF_LENGTH + 1)
    with pytest.raises(InvalidModelRefError):
        normalize_model_ref(too_long)

    # Every individual component (namespace/name <=64, tag <=64) can be
    # independently valid while the ASSEMBLED reference still overflows the
    # 160-char total cap -- the global length check is a real, independent
    # constraint, not just a restatement of the per-component caps.
    namespace = "a" * 64
    name = "b" * 64
    at_cap = f"{namespace}/{name}:{'c' * 30}"  # 64 + 1 + 64 + 1 + 30 == 160
    assert len(at_cap) == MAX_MODEL_REF_LENGTH
    assert normalize_model_ref(at_cap) == at_cap

    over_cap = f"{namespace}/{name}:{'c' * 31}"  # 161
    with pytest.raises(InvalidModelRefError, match="too long"):
        normalize_model_ref(over_cap)
