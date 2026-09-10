"""Project Copilot services."""

from __future__ import annotations

from typing import Any

from frisket.authoring.copilot import copilot_chat, default_copilot_model
from frisket.ai.llm import LLMError
from frisket.ai.llm.remediation import classify_llm_error
from frisket.server.workspace import Workspace


class ProjectCopilotUpstreamError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class ProjectCopilotService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    async def chat(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        router = self._workspace.router_for(project)
        model = body.get("model") or default_copilot_model(router)
        try:
            return await copilot_chat(
                project,
                router,
                body.get("messages", []),
                model=model,
            )
        except LLMError as exc:
            # The remediation seam — never the raw router string: a keyless
            # workspace reads "add a key in Settings → AI Providers", an
            # ollama outage names its URL.
            remedied = classify_llm_error(
                exc,
                provider=model.split("/", 1)[0],
                endpoint_origin=router.local_endpoint_url_for_model(model),
                model=model,
            )
            raise ProjectCopilotUpstreamError(remedied.message) from exc
