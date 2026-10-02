"""Project search services."""

from __future__ import annotations

from typing import Any

from frisket.search import SearchIndexNotReady, fresh_sidecar, search_project_page
from frisket.semantic import resolve_embedder, semantic_search
from frisket.server.workspace import Workspace


class ProjectSearchService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def search(
        self,
        project_id: str,
        *,
        q: str,
        limit: int,
        mode: str,
        rerank: str,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        schedule = getattr(project, "_frisket_schedule_search_index", None)
        if schedule is not None:
            schedule()
        if mode == "semantic":
            # allow_remote=False: project search has no cost gate and no
            # consent dialog, so it must never egress the corpus to a remote
            # embeddings API. With no local model
            # installed resolve_embedder returns None and semantic_search
            # degrades to its lexical path below.
            try:
                with project.read_snapshot() as snapshot:
                    fresh_sidecar(snapshot).close()
                    backend = resolve_embedder(
                        self._workspace.router_for(project), allow_remote=False
                    )
                    embed, embed_id = backend if backend is not None else (None, None)
                    hits = semantic_search(
                        snapshot,
                        q,
                        limit=limit,
                        embed=embed,
                        embed_id=embed_id,
                        rerank=rerank,
                    )
                return {"hits": hits, "indexing": False}
            except SearchIndexNotReady:
                return {"hits": [], "indexing": True}
        page = search_project_page(project, q, limit=limit, rerank=rerank)
        return {"hits": page["hits"], "indexing": not page["complete"]}
