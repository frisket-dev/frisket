"""Project-backed stores hand every caller ITS OWN thread's connection.

``Project.db`` is per-thread on purpose (its own docstring: a single shared
sqlite3 connection races across threads — statement-cache reuse gives
``InterfaceError``/phantom empty fetches). A store that captures
``project.db`` in ``__init__`` silently defeats that, handing every later
caller the CONSTRUCTING thread's connection.

That is not hypothetical: the map runner's cancellation predicate reads
``runs.status`` through ``RunResultStore``, and a recipe running under
``asyncio.to_thread`` (topic segmentation's ``segment``) calls it from a pool
thread while the event-loop thread is mid-query on the same connection. The
run died with ``sqlite3.InterfaceError: bad parameter or other API misuse``,
which is how ``test_temporal_finder_actions`` flaked under ``-n 8``.

The closure test below is the fence: it DISCOVERS the project-backed store
classes rather than listing them, so a new store that caches the connection
turns this red instead of shipping the same defect again.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

import frisket.ai.embeddings.store as embeddings_store
import frisket.engine.store as store_package
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore


def _project(tmp_path: Path) -> Project:
    return Project.create(tmp_path / "affinity.frisket", name="affinity")


def _in_new_thread(call: Any) -> Any:
    """Run ``call`` on a fresh thread and return its value (or re-raise)."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller
            box["error"] = exc

    # Nothing below waits on, or asserts within, a real-time window.
    # realtime: a joined thread is a happens-before rendezvous, not a clock.
    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _project_backed_store_classes() -> list[type]:
    """Every class whose first constructor argument is a ``project`` and which
    exposes a ``db``: the shape that can cache a connection."""
    modules = [store_package, embeddings_store]
    for module_info in pkgutil.iter_modules(store_package.__path__):
        modules.append(
            importlib.import_module(f"{store_package.__name__}.{module_info.name}")
        )
    classes: dict[tuple[str, str], type] = {}
    for module in modules:
        for name, obj in vars(module).items():
            if not inspect.isclass(obj) or obj.__module__ != module.__name__:
                continue
            try:
                params = list(inspect.signature(obj.__init__).parameters.values())[1:]
            except (TypeError, ValueError):  # pragma: no cover — C-level __init__
                continue
            if not params or params[0].name != "project":
                continue
            if any(
                param.default is inspect.Parameter.empty
                and param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)
                for param in params[1:]
            ):
                # Needs a subject beyond the project (RouteStore); not a
                # whole-project store, and it reads ``self._project.db``.
                continue
            # Deliberately NOT filtered on a class-level ``db``: the defect
            # this fences puts ``db`` on the INSTANCE, so filtering here would
            # excuse exactly the shape under test. Stores that expose no
            # connection at all are skipped in the body instead.
            classes[(module.__name__, name)] = obj
    return [classes[key] for key in sorted(classes)]


def test_run_result_store_serves_the_calling_threads_connection(
    tmp_path: Path,
) -> None:
    """The exact seam the queued run flaked on."""
    project = _project(tmp_path)
    store = RunResultStore(project)
    main_connection = project.db

    store_db, thread_db = _in_new_thread(lambda: (store.db, project.db))

    assert store_db is thread_db
    assert store_db is not main_connection


def test_run_result_store_reads_do_not_race_across_threads(tmp_path: Path) -> None:
    """Two threads driving the same store must not corrupt sqlite's statement
    cache. Before the fix this raised ``sqlite3.InterfaceError: bad parameter
    or other API misuse`` well inside the first few hundred iterations."""
    project = _project(tmp_path)
    sheet_id = project.add_sheet("rows")
    op_id = project.append_op("map.template", {"kind": "map.template"})
    store = RunResultStore(project)
    run_id = store.start_run(op_id, sheet_id, "map.template", total_rows=0)
    iterations = 2_000
    failures: list[BaseException] = []
    stop = threading.Event()

    def hammer() -> None:
        try:
            for _ in range(iterations):
                if stop.is_set():
                    return
                store.get_run(run_id)
        except BaseException as exc:  # noqa: BLE001 — reported as the failure
            failures.append(exc)
            stop.set()

    # The assertion is about what sqlite raised, never about elapsed time.
    # realtime: bounded iteration counts, joined to completion — no clock.
    other = threading.Thread(target=hammer)
    other.start()
    try:
        hammer()
    finally:
        other.join()

    assert not failures, f"cross-thread store read raised {failures[0]!r}"


@pytest.mark.parametrize(
    "store_class",
    _project_backed_store_classes(),
    ids=lambda cls: f"{cls.__module__.rsplit('.', 1)[-1]}.{cls.__name__}",
)
def test_project_backed_stores_never_cache_a_connection(
    store_class: type, tmp_path: Path
) -> None:
    """Closure fence: no project-backed store may pin one thread's connection.

    A new store that writes ``self.db = project.db`` in ``__init__`` fails
    here the moment it is added, without anyone re-enumerating the list.
    """
    project = _project(tmp_path)
    store = store_class(project)
    if not hasattr(store, "db"):
        pytest.skip(f"{store_class.__name__} exposes no connection attribute")
    main_connection = project.db

    store_db = _in_new_thread(lambda: store.db)

    assert isinstance(store_db, sqlite3.Connection)
    assert store_db is not main_connection, (
        f"{store_class.__module__}.{store_class.__name__} handed another "
        "thread the constructing thread's sqlite connection"
    )
