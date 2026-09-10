"""The transcription worker must look in the cache diagnostics reports on.

The sandbox re-asserts ``HOME`` to a per-run scratch directory as an isolation
invariant, so a child that resolves the Hugging Face cache itself resolves a
directory that is empty and about to be deleted. On a default install -- none
of ``HF_HUB_CACHE``/``HF_HOME``/``HUGGINGFACE_HUB_CACHE`` set, the resolver
falling back to ``Path.home()`` -- that meant every transcription re-downloaded
the model and discarded it, while ``operability.diagnostics`` probed the real
home and reported the model ready.

The parent resolves once and injects the answer. These tests run a real
sandboxed child through the real policy, because the bug lived in the
interaction between ``env_passthrough`` and the ``HOME`` re-assertion -- a
double that skipped either one would have shown the parent's path and proved
nothing.

subprocess-boundary: env scrubbing across exec is the property under test.
The defect was that the child's environment differs from the parent's, so
only a real child can observe it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from frisket.engine._workers.parakeet_artifacts import huggingface_hub_cache
from frisket.engine.sandbox.shim import SandboxPolicy, run_sandboxed
from frisket.sdk.ops.transcribe_engines import faster_whisper_env, faster_whisper_policy

_RESOLVE = (
    "from frisket.engine._workers.parakeet_artifacts import huggingface_hub_cache;"
    "print(huggingface_hub_cache())"
)

_HUB_VARS = ("HF_HUB_CACHE", "HF_HOME", "HUGGINGFACE_HUB_CACHE")


def _child_resolves(policy: SandboxPolicy, extra_env: dict[str, str]) -> Path:
    result = asyncio.run(
        run_sandboxed(
            # subprocess-boundary: the defect is that the child's env differs from the parent's, observable only across a real exec
            [sys.executable, "-I", "-c", _RESOLVE],
            policy=policy,
            extra_env=extra_env,
        )
    )
    assert result.ok, result.stderr[:400]
    return Path(result.stdout.strip())


def _transcribe_policy_and_env() -> tuple[SandboxPolicy, dict[str, str]]:
    """The policy and env the op itself builds -- not a copy of them.

    A copy would drift, which is the same defect class as the bug under test.
    """
    return faster_whisper_policy(), faster_whisper_env()


@pytest.fixture
def default_install(monkeypatch) -> None:
    """No hub variables set -- the configuration the bug needed."""
    for name in _HUB_VARS:
        monkeypatch.delenv(name, raising=False)


def test_worker_and_diagnostics_resolve_the_same_cache(default_install) -> None:
    policy, extra_env = _transcribe_policy_and_env()

    assert _child_resolves(policy, extra_env) == huggingface_hub_cache()


def test_passing_the_variables_through_would_not_have_worked(default_install) -> None:
    """The shape this replaced, kept as evidence rather than description.

    If this ever starts passing, the sandbox stopped re-asserting HOME and the
    isolation invariant is what needs looking at -- not this test.
    """
    passthrough_only = SandboxPolicy(
        wall_seconds=120,
        env_passthrough=["VIRTUAL_ENV", "PYTHONPATH", *_HUB_VARS],
    )

    assert _child_resolves(passthrough_only, {}) != huggingface_hub_cache()


def test_an_explicit_cache_setting_is_still_honored(monkeypatch, tmp_path) -> None:
    """The parent's resolver reads HF_HUB_CACHE, so injecting it preserves it."""
    chosen = tmp_path / "operator-chosen-cache"
    monkeypatch.setenv("HF_HUB_CACHE", str(chosen))
    policy, extra_env = _transcribe_policy_and_env()

    assert _child_resolves(policy, extra_env) == chosen


def test_the_child_home_really_is_scratch(default_install) -> None:
    """Positive control: the isolation invariant this works around is present.

    Without it the equality assertions above would hold trivially.
    """
    policy, _ = _transcribe_policy_and_env()
    child_home = _child_resolves(
        policy, {"HF_HUB_CACHE": ""}
    )  # empty value falls through to the home-based default

    assert child_home != huggingface_hub_cache()
    assert Path.home() not in child_home.parents
