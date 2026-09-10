"""Separate user-owned correction export from Frisket-directed contribution."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import json
from pathlib import Path
from typing import Any, Protocol


class CorrectionSender(Protocol):
    def send(self, payload: dict[str, Any]) -> None: ...


class CorrectionContributor:
    def __init__(
        self,
        *,
        enabled: bool = False,
        sender: CorrectionSender,
        project_lookup: Callable[[str], Any],
    ) -> None:
        self._enabled = enabled
        self._sender = sender
        self._project_lookup = project_lookup

    def contribute(
        self,
        *,
        project_id: str,
        pairs_factory: Callable[[], list[dict[str, Any]]],
    ) -> bool:
        if not self._enabled:
            return False
        project = self._project_lookup(project_id)
        if project.project_metadata()["sensitive"]:
            return False
        self._sender.send({"pairs": pairs_factory()})
        return True


class CorrectionTrainingExporter:
    """Deliberate offline JSONL export to a caller-owned filesystem target."""

    def __init__(self, *, project_lookup: Callable[[str], Any]) -> None:
        self._project_lookup = project_lookup

    def export(
        self,
        *,
        project_id: str,
        pairs_factory: Callable[[], Iterable[Mapping[str, Any]]],
        target: str | Path,
    ) -> Path:
        # Resolve the project so an invalid id cannot create a plausible export.
        self._project_lookup(project_id)
        path = Path(target)
        with path.open("w", encoding="utf-8") as stream:
            for pair in pairs_factory():
                stream.write(json.dumps(dict(pair), sort_keys=True) + "\n")
        return path
