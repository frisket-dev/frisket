"""Project search services."""

from __future__ import annotations

from typing import Any

from frisket.search import search_project
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
    ) -> list[dict[str, Any]]:
        project = self._workspace.get(project_id)
        if mode == "semantic":
            # allow_remote=False: project search has no cost gate and no
            # consent dialog, so it must never egress the corpus to a remote
            # embeddings API. With no local model
            # installed resolve_embedder returns None and semantic_search
            # degrades to its lexical path below.
            backend = resolve_embedder(
                self._workspace.router_for(project), allow_remote=False
            )
            if backend is not None:
                embed, embed_id = backend
                return semantic_search(
                    project,
                    q,
                    limit=limit,
                    embed=embed,
                    embed_id=embed_id,
                    rerank=rerank,
                )
            return semantic_search(project, q, limit=limit, rerank=rerank)
        return search_project(project, q, limit=limit, rerank=rerank)
