"""HTTP registration for workspace skill admission; compositions supply authority."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request, Response

from frisket.contracts.http.skills import (
    InstructionSkill,
    InstructionSkillList,
    SkillDeleteRequest,
    SkillEnabledRequest,
    SkillSaveRequest,
    SkillUpdateRequest,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.skills import (
    SkillLibrary,
    SkillLibraryError,
    SkillNotFound,
    SkillRevisionConflict,
)


RequireManager = Callable[[Request], None]


def _skill_error(exc: SkillLibraryError) -> HTTPException:
    return HTTPException(exc.status_code, str(exc))


def register_skill_routes(
    app: FastAPI,
    *,
    library: SkillLibrary,
    require_manager: RequireManager,
) -> None:
    """Register text-only skills using a composition-owned authority check.

    Solo passes its workspace-owner check. Team must pass a per-organization
    admin check and a library constructed for that same organization root.
    """

    errors = http_error_responses(401, 403, 404, 409, 415, 422, 500)

    @app.get("/api/skills", response_model=InstructionSkillList, responses=errors)
    def list_skills(request: Request) -> InstructionSkillList:
        require_manager(request)
        try:
            return InstructionSkillList.model_validate(library.list())
        except SkillLibraryError as exc:
            raise _skill_error(exc) from exc

    @app.post("/api/skills", response_model=InstructionSkill, responses=errors)
    def create_skill(request: Request, body: SkillSaveRequest) -> InstructionSkill:
        require_manager(request)
        try:
            return InstructionSkill.model_validate(library.create(body.content, enabled=body.enabled))
        except SkillLibraryError as exc:
            raise _skill_error(exc) from exc

    @app.post("/api/skills/upload", response_model=InstructionSkill, responses=errors)
    async def upload_skill(request: Request) -> InstructionSkill:
        require_manager(request)
        content_type = request.headers.get("content-type", "").lower()
        if not (content_type.startswith("text/markdown") or content_type.startswith("text/plain")):
            raise HTTPException(415, "Upload one UTF-8 SKILL.md text file; ZIP packages are unsupported.")
        try:
            content = (await request.body()).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(422, "SKILL.md must be UTF-8 text.") from exc
        try:
            return InstructionSkill.model_validate(library.create(content))
        except SkillLibraryError as exc:
            raise _skill_error(exc) from exc

    @app.put("/api/skills/{skill_id}", response_model=InstructionSkill, responses=errors)
    def update_skill(
        skill_id: str, request: Request, body: SkillUpdateRequest
    ) -> InstructionSkill:
        require_manager(request)
        try:
            return InstructionSkill.model_validate(
                library.update(skill_id, expected_revision=body.expected_revision, content=body.content)
            )
        except SkillLibraryError as exc:
            raise _skill_error(exc) from exc

    @app.patch("/api/skills/{skill_id}/enabled", response_model=InstructionSkill, responses=errors)
    def set_skill_enabled(
        skill_id: str, request: Request, body: SkillEnabledRequest
    ) -> InstructionSkill:
        require_manager(request)
        try:
            return InstructionSkill.model_validate(
                library.set_enabled(skill_id, expected_revision=body.expected_revision, enabled=body.enabled)
            )
        except SkillLibraryError as exc:
            raise _skill_error(exc) from exc

    @app.delete("/api/skills/{skill_id}", status_code=204, responses=errors)
    def delete_skill(skill_id: str, request: Request, body: SkillDeleteRequest) -> Response:
        require_manager(request)
        try:
            library.delete(skill_id, expected_revision=body.expected_revision)
        except SkillLibraryError as exc:
            raise _skill_error(exc) from exc
        return Response(status_code=204)
