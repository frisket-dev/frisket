"""Action-registry catalog access for the action-panel flight report.

Loads the merged root action catalog
and pairs each entry with the ribbon "launcher kind" used to open its
configuration panel in the Workbench UI, when one exists.

The catalog-kind -> launcher-kind mapping mirrors
``web/src/actions/registry.ts``'s ACTION_BINDINGS for the generic action forms
this report captures. The e2e ``openAction()`` helper discovers each kind's
current ribbon category from the rendered UI, so moving an action does not
require a second location table here. Notably ``export.column_tables`` uses a
dedicated Export modal / pdf-tables picker / JSON column menu rather than the
generic action form, so it remains deliberately absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from frisket.actions.system import root_action_catalog

# catalogKind -> launcherKind, restricted to kinds reachable via a ribbon tile.
CATALOG_KIND_TO_LAUNCHER_KIND: dict[str, str] = {
    "map.classify": "classify",
    "map.extract": "extract",
    "map.summarize": "summarize",
    "map.translate": "translate",
    "research.web_search": "research.web_search",
    "research.answer": "agent",
    "media.ytdlp_download": "download_media",
    "web.capture_page": "capture_url",
    "derive.table_from_list": "derive",
    "media.extract_pdf_tables": "pdf_tables",
    "reduce.group_summary": "reduce",
    "join.semantic": "semantic_join",
    "derive.join": "derive_join",
    "map.python": "python",
    "map.judge": "judge",
    "map.ner": "ner",
    "enrich.geocode": "geocode",
    "enrich.census_demographics": "census_demographics",
}


@dataclass
class ActionEntry:
    kind: str
    title: str
    description: str
    launcher_kind: str | None
    required_capabilities: list[str] = field(default_factory=list)
    required_credentials: list[str] = field(default_factory=list)
    writes_project: bool = False

    @property
    def ribbon_launchable(self) -> bool:
        return self.launcher_kind is not None


def load_catalog() -> list[ActionEntry]:
    """Return every action, in catalog order, with its launcher kind (if any)."""
    payload: dict[str, Any] = root_action_catalog().model_dump(mode="json")
    entries: list[ActionEntry] = []
    for action in payload["actions"]:
        kind = action["kind"]
        entries.append(
            ActionEntry(
                kind=kind,
                title=action.get("title") or kind,
                description=action.get("description") or "",
                launcher_kind=(
                    kind
                    if action.get("ui_hints", {}).get("form") == "generated"
                    else CATALOG_KIND_TO_LAUNCHER_KIND.get(kind)
                ),
                required_capabilities=list(action.get("required_capabilities", [])),
                required_credentials=list(action.get("required_credentials", [])),
                writes_project=bool(action.get("writes_project", False)),
            )
        )
    return entries
