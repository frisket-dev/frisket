from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.testing import ensure_isolated_llm_cache
from helpers import make_client, replay_router

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHECKLOG = ROOT / ".frisket" / "check-runs.jsonl"


# ---------------------------------------------------------------------------
# Hermetic environment
# ---------------------------------------------------------------------------
#
# `magika/__init__.py` -- a transitive dependency of markitdown, which
# `media.to_markdown` imports -- runs `dotenv.load_dotenv(dotenv.find_dotenv())`
# at IMPORT time. `find_dotenv()` walks up from magika's own file inside the
# venv and lands on this repo's real `.env`, so the moment any test touches
# that import chain the operator's LIVE provider keys and hosted locators are
# in `os.environ` for every remaining test on that worker. CI has no `.env`
# beside its venv, so the divergence is invisible there; locally it means a
# "keyless" or "cached" test can make a real billable call, and a failing
# assertion can print a production password in its `os.environ` repr (observed
# 2026-07-26 while closing the base-side half of the same filing).
#
# Two layers, because the leak has two shapes:
#
# 1. `_neutralize_ambient_dotenv()` stops the injection at its source. No
#    product code in this repo calls `load_dotenv` (grepped 2026-07-26), so
#    every call in this process is a dependency side effect and a no-op is the
#    honest stub. This layer needs no list of names: it is hermetic against ANY
#    ambient `.env`.
# 2. `hermetic_deployment_env` strips the deployment names before each test
#    anyway, so a developer whose shell exports them (direnv does, from the
#    same file) still measures what CI measures.
#
# The app's own runtime `.env` use is untouched: this is the TEST process.
# Nothing here blocks an explicit per-test `monkeypatch.setenv` -- the strip
# runs at setup, before the test body, and covers the AMBIENT environment only.

# Deployment names carried by this repo's own `.env`, taken from that file
# rather than guessed. A leaked one does not merely add capability: it points
# the app somewhere real.
_DEPLOYMENT_ENV_NAMES: frozenset[str] = frozenset(
    {
        "FRISKET_BASE_URL",
        "FRISKET_BUILD_SHA",
        "FRISKET_DATABASE_ADMIN_URL",
        "FRISKET_EMAIL_FROM_ADDRESS",
        "FRISKET_EMAIL_FROM_NAME",
        "FRISKET_RUN_QUEUE_DATABASE_URL",
        "FRISKET_SECRETS_MASTER_KEY",
        "FRISKET_TEAM_DATABASE_URL",
        "POSTGRES_PASSWORD",
        "SENTRY_DSN",
    }
)

_DEPLOYMENT_ENV_PREFIXES: tuple[str, ...] = (
    "FRISKET_MAP_",
    "GOOGLE_OAUTH_",
    "LITESTREAM_",
    "MODAL_TOKEN_",
)

# Credentials by SHAPE rather than by vendor list: the `.env` carries ~25
# provider keys and the next one added must be covered without editing this
# file.
_CREDENTIAL_ENV_SUFFIXES: tuple[str, ...] = (
    "_API_KEY",
    "_API_TOKEN",
    "_ACCESS_KEY_ID",
    "_SECRET_ACCESS_KEY",
    "_TOKEN",
)

# Test-owned selectors deliberately NOT stripped: they are how an operator opts
# INTO a heavier gate, and a stripped one would silently skip the gate it was
# set to run.
_TEST_SELECTOR_ENV_NAMES: frozenset[str] = frozenset(
    {
        "FRISKET_CHECKLOG_DISABLE",
        "FRISKET_CHECKLOG_PATH",
        "FRISKET_E2E_WEATHERAPI_KEY",
        "FRISKET_LIVE_GROUNDING_PROJECT",
        "FRISKET_NOTIFICATION_LIVE_PROVIDER_TESTS",
        "FRISKET_OCR_EXPECTED_SOURCE_SHA",
        "FRISKET_OLLAMA_LIVE",
        "FRISKET_PG_TEST_URL",
    }
)


def _is_deployment_env(name: str) -> bool:
    if name in _TEST_SELECTOR_ENV_NAMES:
        return False
    return (
        name in _DEPLOYMENT_ENV_NAMES
        or name.startswith(_DEPLOYMENT_ENV_PREFIXES)
        or name.endswith(_CREDENTIAL_ENV_SUFFIXES)
    )


def _deployment_env_names(env: Any) -> list[str]:
    return [name for name in list(env) if _is_deployment_env(name)]


def _strip_deployment_env() -> None:
    for name in _deployment_env_names(os.environ):
        os.environ.pop(name, None)


def _neutralize_ambient_dotenv() -> None:
    """Make `dotenv.load_dotenv` a no-op for the whole test session.

    Patched on the module object BEFORE anything imports magika, so magika's
    `import dotenv` / `load_dotenv(...)` at import time reaches the stub.
    """
    try:
        import dotenv
        import dotenv.main
    except ModuleNotFoundError:  # pragma: no cover - dotenv is transitive
        return

    def _no_ambient_env(*_args: Any, **_kwargs: Any) -> bool:
        return False

    dotenv.load_dotenv = _no_ambient_env
    dotenv.main.load_dotenv = _no_ambient_env


_neutralize_ambient_dotenv()


@pytest.fixture(autouse=True)
def hermetic_deployment_env() -> None:
    """Strip ambient deployment names before every test.

    Declared ahead of ``restore_process_env`` on purpose: that fixture's
    snapshot is then taken from the already-stripped environment, so its
    restore cannot put a leaked key back for the next test.

    Deliberately NOT a monkeypatch-scoped restore: a test that legitimately
    wants one of these sets it itself, and nothing in the suite should observe
    the operator's real deployment.
    """
    _strip_deployment_env()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Plain server client (local-tier default router)."""
    return make_client(tmp_path)


@pytest.fixture
def replay_client(tmp_path: Path) -> TestClient:
    """Server client wired to the committed cache in replay_strict mode."""
    return make_client(tmp_path, router=replay_router())


@pytest.fixture(autouse=True)
def restore_process_env():
    """Undo any process-environment mutation a test leaves behind.

    Product code legitimately writes os.environ: cli.py's ``_standalone_env``
    sets the seven locators the standalone app and worker processes inherit,
    and frisket.testing pins the cache locator. A test that drives those paths
    in-process leaves the mutation in the interpreter, and under
    ``-n auto --dist loadfile`` every later FILE on that worker inherits it --
    typically pointing at a tmp_path that no longer exists.

    That is why whole-suite runs failed on a set of tests that changed from run
    to run while every one of them passed in isolation: which tests break
    depends on file-to-worker assignment, so it tracks the runner's core count.
    CI stayed green only because its assignment happened to keep polluter and
    victim apart. Snapshot-and-restore makes env isolation a property of the
    harness instead of luck.
    """
    snapshot = dict(os.environ)
    yield
    if os.environ != snapshot:
        os.environ.clear()
        os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def restore_column_type_registry():
    """Undo any column-type registration a test leaves behind.

    ``frisket.authoring.column_types`` keeps a process-global registry, and
    plugin activation tests (stars, case_files, ndjson cases, backend
    contribution registry) register plugin types through the real backend
    seam. Without cleanup, every later test in the same process sees the
    leaked types, so registry-parity assertions (e.g. run.backfill's
    accepted_column_types) hold or fail depending on collection order.
    Snapshot-and-restore makes registry isolation a property of the harness
    instead of collection-order luck.
    """
    from frisket.authoring import column_types as _column_types

    with _column_types._lock:
        snapshot = dict(_column_types._registry)
    yield
    with _column_types._lock:
        if _column_types._registry != snapshot:
            _column_types._registry.clear()
            _column_types._registry.update(snapshot)


@pytest.fixture(autouse=True)
def restore_installed_pricing_policy():
    """Undo any pricing policy a test installs.

    ``frisket.execution.pricing_policy`` keeps a process-global installed
    policy — it has to, because the fourteen sites that mint a money
    confirmation share no object to carry one (see that module). A test that
    installs a cost-plus tariff and does not clear it leaves every later FILE
    on the same ``-n auto --dist loadfile`` worker rating at 2x, which reads as
    "the consent hash moved" in suites that never mention pricing at all.

    Same reasoning, and the same snapshot-and-restore shape, as
    ``restore_process_env`` and ``restore_column_type_registry`` above: process
    isolation belongs to the harness, not to every test author remembering a
    teardown.
    """
    from frisket.execution import pricing_policy as _pricing_policy

    _pricing_policy._reset_pricing_policy_for_tests()
    yield
    _pricing_policy._reset_pricing_policy_for_tests()


@pytest.fixture(autouse=True)
def restore_installed_plugin_composition_policy():
    """Undo any plugin composition policy a test installs.

    ``frisket.authoring.workbench.plugin_runtime_shared`` keeps a process-global
    installed policy (the pricing-policy seam's shape and reasoning), so a test
    that installs a restricted policy and does not clear it would leave every
    later FILE on the same worker exposing (or hiding) plugins it never asked to
    -- process isolation belongs to the harness, not to every test author.
    """
    from frisket.authoring.workbench import plugin_runtime_shared as _shared

    _shared._reset_plugin_composition_policy_for_tests()
    yield
    _shared._reset_plugin_composition_policy_for_tests()


def _plugin_registry_state() -> tuple[Any, ...] | None:
    """Contents of the process-global plugin registry, or None if unbuilt."""
    from frisket.authoring import plugin_registry as _plugin_registry

    registry = _plugin_registry._DEFAULT
    if registry is None:
        return None
    with registry._lock:
        return (
            registry,
            dict(registry._recipes),
            dict(registry._importers),
            dict(registry._job_handlers),
            dict(registry._plugin_manifests),
            {
                binding_type: dict(bindings)
                for binding_type, bindings in registry._runtime_bindings.items()
            },
        )


def _restore_plugin_registry_state(snapshot: tuple[Any, ...] | None) -> None:
    from frisket.authoring import plugin_registry as _plugin_registry

    if snapshot is None:
        # Nothing had built the singleton yet; drop whatever this test built so
        # the next reader lazily rebuilds the pristine builtin set.
        with _plugin_registry._DEFAULT_LOCK:
            _plugin_registry._DEFAULT = None
        return
    registry, recipes, importers, job_handlers, manifests, runtime_bindings = snapshot
    with registry._lock:
        registry._recipes.clear()
        registry._recipes.update(recipes)
        registry._importers.clear()
        registry._importers.update(importers)
        registry._job_handlers.clear()
        registry._job_handlers.update(job_handlers)
        registry._plugin_manifests.clear()
        registry._plugin_manifests.update(manifests)
        for binding_type, bindings in runtime_bindings.items():
            registry._runtime_bindings[binding_type].clear()
            registry._runtime_bindings[binding_type].update(bindings)
    with _plugin_registry._DEFAULT_LOCK:
        # A test may have swapped the singleton itself (_reset_default_registry_
        # for_tests sets it to None); put the object this test started with back.
        _plugin_registry._DEFAULT = registry


@pytest.fixture(autouse=True)
def restore_plugin_registry():
    """Undo any plugin registration a test leaves in the global registry.

    ``frisket.authoring.plugin_registry._DEFAULT`` is a process-wide singleton
    holding recipes, importers, job handlers, loaded plugin manifests and the
    runtime-binding table. It has to be process-global: plugin activation
    happens inside one request and dispatch reads it from a dozen unrelated
    call sites (querysets, action catalog, map points, projections) that have
    no registry to be handed. So it cannot be scoped to a caller, and the
    isolation has to live in the harness.

    Activating a plugin through the real seam writes into it permanently.
    ``tests/server/test_server_workbench_routes.py::
    test_plugin_index_bootstraps_bundled_plugins_for_legacy_projects`` opts out
    of ``hermetic_bundled_plugins_root`` and installs the REAL bundled tree, so
    it left ``frisket.geo``/``frisket.transliterate`` manifests plus their
    action and projection runtime bindings registered for the rest of the
    process. Every later test that walks ``runtime_binding_specs`` then sees a
    plugin no test asked for -- which is how
    ``tests/ops/test_datalab_hosted_engines.py::
    test_project_action_catalog_payload_threads_org_provider_keys`` started
    dereferencing ``project.db`` on a stub project in the combined run while
    passing in isolation.

    Snapshot-and-restore makes registry isolation a property of the harness
    instead of collection-order luck, exactly as restore_column_type_registry
    does for the sibling column-type registry.
    """
    from frisket.authoring import plugin_registry as _plugin_registry

    snapshot = _plugin_registry_state()
    with _plugin_registry._TRUSTED_BACKEND_HANDLERS_LOCK:
        trusted = dict(_plugin_registry._TRUSTED_BACKEND_HANDLERS)
    yield
    _restore_plugin_registry_state(snapshot)
    with _plugin_registry._TRUSTED_BACKEND_HANDLERS_LOCK:
        if _plugin_registry._TRUSTED_BACKEND_HANDLERS != trusted:
            _plugin_registry._TRUSTED_BACKEND_HANDLERS.clear()
            _plugin_registry._TRUSTED_BACKEND_HANDLERS.update(trusted)


def _global_registry_census() -> dict[str, tuple[str, ...]]:
    """The names in every process-global registry the harness isolates.

    Limited to the two registries whose full contents a fresh process already
    holds -- ``default_registry()`` registers its builtins on construction and
    ``column_types`` registers its core set at import -- so taking the census
    is enough to make the baseline complete. The sibling tables
    (``server.sources.runtime._POLLERS``, ``url_classification._MATCHERS``) are
    filled LAZILY by product composition (``ensure_rss_poller``), so a census
    of those would report first-party registration as drift.
    """
    from frisket.authoring import column_types as _column_types
    from frisket.authoring import plugin_registry as _plugin_registry

    registry = _plugin_registry.default_registry()
    with registry._lock:
        census = {
            "recipes": tuple(sorted(registry._recipes)),
            "importers": tuple(sorted(registry._importers)),
            "job handlers": tuple(sorted(registry._job_handlers)),
            "plugin manifests": tuple(sorted(registry._plugin_manifests)),
            **{
                f"{binding_type} runtime bindings": tuple(sorted(bindings))
                for binding_type, bindings in registry._runtime_bindings.items()
            },
        }
    with _plugin_registry._TRUSTED_BACKEND_HANDLERS_LOCK:
        census["trusted backend handlers"] = tuple(
            sorted(_plugin_registry._TRUSTED_BACKEND_HANDLERS)
        )
    with _column_types._lock:
        census["column types"] = tuple(sorted(_column_types._registry))
    return census


_REGISTRY_CENSUS_AT_STARTUP: dict[str, tuple[str, ...]] = {}


@pytest.fixture(scope="session", autouse=True)
def no_global_registry_drift_across_the_session():
    """Fail the session if a process-global registry ends it changed.

    The function-scoped fixtures above restore these registries at each test's
    teardown, which is where activation leaks come from today. They cannot see
    a write made from module or session scope, or at import time by a test
    module -- those happen outside any function fixture, so every later
    snapshot already contains the leak and "restore" preserves it forever.
    This is the backstop for that hole, and it is the whole check: one census
    of names taken in pytest_configure (before any test module is imported)
    and compared after the last test.
    """
    before = dict(_REGISTRY_CENSUS_AT_STARTUP)
    yield
    after = _global_registry_census()
    drift = [
        f"{name}: {sorted(set(after[name]) - set(values))} appeared, "
        f"{sorted(set(values) - set(after[name]))} vanished"
        for name, values in before.items()
        if after[name] != values
    ]
    assert not drift, (
        "process-global registry state drifted across the session: "
        + "; ".join(drift)
        + " -- something registered it outside a function-scoped fixture, so "
        "restore_plugin_registry/restore_column_type_registry could not undo it."
    )


@pytest.fixture(scope="module", autouse=True)
def hermetic_bundled_plugins_root_for_module_fixtures(
    tmp_path_factory: pytest.TempPathFactory,
):
    """Pin bundled packages empty before any module-scoped app is built.

    Module fixtures run before function fixtures. A module-scoped fixture that
    constructs an app therefore seeds the real bundled packages into the
    process-global registry before the per-test registry snapshot exists; all
    later snapshots preserve that leak. The module pin makes app construction
    hermetic at both fixture scopes. Function tests that deliberately exercise
    shipped packages override it below.
    """
    from frisket.authoring.workbench import plugin_runtime as _plugin_runtime
    from frisket.authoring.workbench import (
        plugin_runtime_status as _plugin_runtime_status,
    )

    empty_root = tmp_path_factory.mktemp("module-empty-bundled-plugins")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(_plugin_runtime, "_bundled_plugins_root", lambda: empty_root)
    monkeypatch.setattr(
        _plugin_runtime_status, "_bundled_plugins_root", lambda: empty_root
    )
    try:
        yield
    finally:
        monkeypatch.undo()


@pytest.fixture(autouse=True)
def hermetic_bundled_plugins_root(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
):
    """Expose the real bundled tree only to explicitly marked tests.

    Real bundled packages ship in src/frisket/authoring/bundled_plugins/ (frisket_geo,
    geo-bundled-plugin-v1) and project bootstrap seeds every one of them into
    every server-created project (workspace.create ->
    bootstrap_project_bundled_plugins). Without this pin, every test that
    POSTs /api/projects would install+activate the real geo plugin, breaking
    the suite-wide "a fresh project has no plugins" assumption
    (tests/test_plugin_bundled_install_path.py's fixture docstring) and
    coupling hundreds of unrelated tests to the shipped package set.

    The REAL tree is exercised deliberately: tests/test_workbench_plugin_geo_bundled.py
    re-points the root at src/frisket/authoring/bundled_plugins/ (its
    ``real_bundled_root`` fixture), and the entire Playwright stack runs the
    real server with the real tree. This fixture is suite hygiene, not a
    product-path bypass.
    """
    from frisket.authoring.workbench import plugin_runtime as _plugin_runtime
    from frisket.authoring.workbench import (
        plugin_runtime_status as _plugin_runtime_status,
    )

    if request.node.get_closest_marker("real_bundled_plugins"):
        real_root = Path(_plugin_runtime_status.__file__).resolve().parent.parent / (
            "bundled_plugins"
        )
        monkeypatch.setattr(_plugin_runtime, "_bundled_plugins_root", lambda: real_root)
        monkeypatch.setattr(
            _plugin_runtime_status, "_bundled_plugins_root", lambda: real_root
        )
    yield


def pytest_configure(config):
    # Copy the committed real-response cache once per pytest run. Tests use the
    # temp path via FRISKET_TEST_CACHE_PATH, so replay/fresh writes do not dirty
    # tests/cache/llm_cache.db unless FRISKET_CACHE_REFRESH=1 is explicit.
    ensure_isolated_llm_cache()
    # Before collection, so a registration made at test-module IMPORT time
    # counts as drift (see no_global_registry_drift_across_the_session).
    _REGISTRY_CENSUS_AT_STARTUP.update(_global_registry_census())


def _targeted_specific_nodeids(config) -> bool:
    """True when the invocation named at least one ``::`` nodeid.

    The caller then wants exactly those tests, so collection is left alone.
    """
    candidates: list[object] = []
    inv = getattr(config, "invocation_params", None)
    if inv is not None:
        candidates.extend(inv.args)
    candidates.extend(getattr(config, "args", None) or [])
    return any("::" in str(a) for a in candidates)


def _explicitly_requested_gap(config) -> bool:
    try:
        expr = config.getoption("-m") or ""
    except (ValueError, KeyError):
        expr = ""
    # gap_env names a distinct designated-environment-proof marker whose text
    # embeds "gap" (and "not gap_env" embeds the literal substring "not gap"),
    # so it must be stripped before the substring checks below -- otherwise
    # both the CI selector `-m "gap and not gap_env"` and a bare `-m
    # "not gap_env"` would misread as the negative default and deselect every
    # gap test, including the ones the expression asked for.
    stripped = re.sub(r"(?<!\w)gap_env(?!\w)", "", expr)
    # honor an explicit positive request like `-m gap`, but not the negative
    # default `-m "not gap and not network"`.
    return "gap" in stripped and "not gap" not in stripped


def pytest_collection_modifyitems(config, items):
    if _targeted_specific_nodeids(config) or _explicitly_requested_gap(config):
        return
    keep, deselected = [], []
    for it in items:
        (deselected if it.get_closest_marker("gap") else keep).append(it)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = keep


TESTS_ROOT = Path(__file__).resolve().parent

# Not how "full suite" is detected — the selected roots do that. This only
# spares a whole-tree invocation that a filter narrowed to a small subset,
# such as CI's serial `pytest -q -m "gap and not gap_env" tests/` (128 of
# 7876) or a local `pytest tests/ -k workbench`. Those select the tree but
# run a few minutes at most.
_SERIAL_FULL_SUITE_THRESHOLD = 2000


def _selects_whole_tree(config) -> bool:
    """True when the invocation's selection roots cover the entire suite.

    `config.args` holds those roots: the command line's positional paths, or
    `testpaths` when it gave none. A root that IS the tests directory (or an
    ancestor of it) selects everything; a subdirectory, file, or nodeid does
    not. Item count cannot tell those apart — `tests/engine` alone collects
    2765, which is why the count heuristic this replaced refused a legitimate
    targeted run.
    """
    args = [str(a) for a in (getattr(config, "args", None) or [])]
    if not args:
        return True
    invocation = getattr(getattr(config, "invocation_params", None), "dir", None)
    base = Path(str(invocation)) if invocation else TESTS_ROOT.parent
    for arg in args:
        # `path::nodeid` selects inside one file, never the tree.
        path = Path(arg.split("::", 1)[0])
        root = (path if path.is_absolute() else base / path).resolve()
        if root == TESTS_ROOT or root in TESTS_ROOT.parents:
            return True
    return False


def pytest_collection_finish(session):
    """Refuse a serial full-suite run: it takes ~35 min where -n 8 takes ~5.

    The parallel invocation is this suite's own documented gate (CI runs
    `-n auto --dist loadfile`; conftest is hardened for xdist). Debugging a
    single file or directory serially is fine — the guard fires only when the
    invocation actually selects the whole tree, with no xdist controller or
    worker active. Escape hatch: FRISKET_ALLOW_SERIAL_FULL_SUITE=1."""

    config = session.config
    if config.option.collectonly:
        return
    if os.environ.get("FRISKET_ALLOW_SERIAL_FULL_SUITE") == "1":
        return
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    if getattr(config.option, "numprocesses", None):
        return
    if not _selects_whole_tree(config):
        return
    if len(session.items) <= _SERIAL_FULL_SUITE_THRESHOLD:
        return
    raise pytest.UsageError(
        f"refusing to run {len(session.items)} tests serially (~35 min): use "
        "`pytest -q -n 8 --dist loadfile` (~5 min), or set "
        "FRISKET_ALLOW_SERIAL_FULL_SUITE=1 to override deliberately"
    )


def _checklog_disabled() -> bool:
    return os.environ.get("FRISKET_CHECKLOG_DISABLE") == "1"


def _checklog_path() -> Path:
    raw = os.environ.get("FRISKET_CHECKLOG_PATH")
    return Path(raw).expanduser() if raw else DEFAULT_CHECKLOG


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _append_checklog(record: dict[str, Any]) -> None:
    if _checklog_disabled():
        return
    path = _checklog_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    except OSError as exc:
        print(f"[checklog] unable to write {path}: {exc}", file=sys.stderr)


def pytest_sessionstart(session):
    # realtime: this is the real session clock, not a product assertion
    session.config._frisket_checklog_start = time.monotonic()
    session.config._frisket_checklog_started_at = _iso_now()


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    # realtime: see pytest_sessionstart — real wall time is the point.
    started = getattr(config, "_frisket_checklog_start", time.monotonic())
    started_at = getattr(config, "_frisket_checklog_started_at", _iso_now())
    inv = getattr(config, "invocation_params", None)
    args = [str(a) for a in (getattr(inv, "args", None) or sys.argv[1:])]
    selected = [a for a in args if not a.startswith("-")]
    label = " ".join(selected) if selected else "pytest"
    returncode = int(exitstatus)
    _append_checklog(
        {
            "schema_version": 1,
            "source": "pytest",
            "kind": "pytest",
            "label": label,
            "command": ["pytest", *args],
            "cwd": str(Path.cwd()),
            "outcome": "PASS" if returncode == 0 else "FAIL",
            "returncode": returncode,
            # realtime: see pytest_sessionstart — real wall time is the point.
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "started_at": started_at,
            "finished_at": _iso_now(),
            "metadata": {"tests_collected": getattr(session, "testscollected", None)},
        }
    )
