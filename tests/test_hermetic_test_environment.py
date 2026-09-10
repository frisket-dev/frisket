"""The suite does not observe the operator's deployment environment.

Two mechanisms can put this repo's real `.env` into `os.environ` mid-run:

* a dependency calling `dotenv.load_dotenv(dotenv.find_dotenv())` at import
  time — `magika/__init__.py` does exactly this, and markitdown (which
  `media.to_markdown` imports) pulls magika in, so any test that touches that
  chain injects the file for every later test on the worker; and
* a developer whose shell exports the same names (direnv reads that file too).

Either one puts LIVE provider keys in front of tests that believe they are
keyless or cache-only, and puts a production password into the `os.environ`
repr any failing assertion prints. `tests/conftest.py` closes both. These
checks execute against the live process environment rather than reading
conftest as text, which is what stops the fixture from being quietly deleted.
"""

from __future__ import annotations

import os

import pytest

from conftest import _deployment_env_names, _is_deployment_env


def test_no_deployment_credential_is_visible_to_a_test() -> None:
    leaked = sorted(_deployment_env_names(os.environ))
    assert leaked == [], (
        f"the test process can see deployment environment {leaked}; the "
        "hermetic_deployment_env fixture in tests/conftest.py is not running"
    )


def test_importing_markitdown_cannot_inject_the_repo_dotenv() -> None:
    """The exact reported path, executed rather than asserted about.

    `import markitdown` reaches magika's import-time `load_dotenv`. With the
    neutralization in place the import is a no-op for the environment, so a
    test that runs after `media.to_markdown` sees no OPENAI_API_KEY it did not
    set itself.
    """
    pytest.importorskip("markitdown")
    before = dict(os.environ)
    import markitdown  # noqa: F401,PLC0415

    assert dict(os.environ) == before
    assert "OPENAI_API_KEY" not in os.environ


def test_dotenv_loading_is_neutralized_for_the_session() -> None:
    """Call the real entry point against this repo's own `.env` and require
    that nothing lands in `os.environ`."""
    dotenv = pytest.importorskip("dotenv")

    before = dict(os.environ)
    assert dotenv.load_dotenv(dotenv.find_dotenv()) is False
    assert dict(os.environ) == before


def test_an_explicit_per_test_key_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strip covers the AMBIENT environment only.

    Many tests configure a provider by setting its key themselves; the
    fixture runs at setup, before the test body, so an explicit set is
    untouched. Without this, closing the leak would have broken the suite's
    own way of exercising keyed paths.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-explicitly-set-by-this-test")
    assert os.environ["OPENAI_API_KEY"] == "sk-explicitly-set-by-this-test"


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DATALAB_API_KEY",
        "HF_TOKEN",
        "MODAL_TOKEN_SECRET",
        "LITESTREAM_BUCKET",
        "FRISKET_SECRETS_MASTER_KEY",
        "FRISKET_RUN_QUEUE_DATABASE_URL",
        "POSTGRES_PASSWORD",
        "GOOGLE_OAUTH_CLIENT_SECRET",
    ],
)
def test_deployment_names_are_classified(name: str) -> None:
    assert _is_deployment_env(name)


@pytest.mark.parametrize(
    "name",
    [
        "FRISKET_PG_TEST_URL",
        "FRISKET_OLLAMA_LIVE",
        "FRISKET_E2E_WEATHERAPI_KEY",
        "FRISKET_CACHE_MODE",
        "PATH",
        "HOME",
        "VIRTUAL_ENV",
    ],
)
def test_test_selectors_and_ordinary_environment_survive(name: str) -> None:
    assert not _is_deployment_env(name)
