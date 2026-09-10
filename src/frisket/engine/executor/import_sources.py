"""Request-scoped bindings for admitted import sources."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from typing import Iterator, Mapping

from frisket.engine.executor.action_inventory import BoundLocalFile, ExecutorDeps
from frisket.engine.executor.local_file_read import AdmittedLocalFileReader


def bind_import_sources(
    deps: ExecutorDeps | None = None,
    *,
    local_files: Mapping[str, BoundLocalFile] | None = None,
) -> ExecutorDeps:
    """Return dependency bindings with the supplied admitted sources added."""

    deps = deps if deps is not None else ExecutorDeps()
    changes = {}
    if local_files is not None:
        changes["local_file_sources"] = {
            **(deps.local_file_sources or {}),
            **local_files,
        }
    return replace(deps, **changes)


@contextmanager
def open_local_file_reader(
    sources: Mapping[str, BoundLocalFile] | None = None,
) -> Iterator[AdmittedLocalFileReader]:
    """Open a reader while leaving borrowed source streams to their caller."""

    reader = AdmittedLocalFileReader(sources)
    try:
        yield reader
    finally:
        reader.close()
