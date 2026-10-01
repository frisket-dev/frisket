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
    MAX_SKILL_BYTES,
    SkillLibrary,
    SkillLibraryError,
)


RequireManager = Callable[[Request], None]
SkillLibraryFor = SkillLibrary | Callable[[Request], SkillLibrary]


def _skill_error(exc: SkillLibraryError) -> HTTPException:
    return HTTPException(exc.status_code, str(exc))


def register_skill_routes(
    app: FastAPI,
    *,
    library: SkillLibraryFor,
    require_manager: RequireManager,
) -> None:
    """Register text-only skills using a composition-owned authority check.

    Solo passes one workspace library. Team supplies a factory that resolves a
    library for the authenticated organization only after its admin check.
    """

    errors = http_error_responses(401, 403, 404, 409, 415, 422, 500)

    def managed_library(request: Request) -> SkillLibrary:
        require_manager(request)
        return library(request) if callable(library) else library

    @app.get("/api/skills", response_model=InstructionSkillList, responses=errors)
    def list_skills(request: Request) -> InstructionSkillList:
        selected_library = managed_library(request)
        try:
            return InstructionSkillList.model_validate(selected_library.list())
        except SkillLibraryError as exc:
            raise _skill_error(exc) from exc

    @app.post("/api/skills", response_model=InstructionSkill, responses=errors)
    def create_skill(request: Request, body: SkillSaveRequest) -> InstructionSkill:
        selected_library = managed_library(request)
        try:
            return InstructionSkill.model_validate(
                selected_library.create(body.content, enabled=body.enabled)
            )
        except SkillLibraryError as exc:
            raise _skill_error(exc) from exc

    @app.post("/api/skills/upload", response_model=InstructionSkill, responses=errors)
    async def upload_skill(request: Request) -> InstructionSkill:
        selected_library = managed_library(request)
        content_type = request.headers.get("content-type", "").lower()
        if not (
            content_type.startswith("text/markdown")
            or content_type.startswith("text/plain")
        ):
            raise HTTPException(
                415,
                "Upload one UTF-8 SKILL.md text file; ZIP packages are unsupported.",
            )
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_SKILL_BYTES:
                    raise HTTPException(413, "SKILL.md must be at most 128 KiB.")
            except ValueError as exc:
                raise HTTPException(
                    422, "SKILL.md has an invalid content length."
                ) from exc
        chunks: list[bytes] = []
        size = 0
        try:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_SKILL_BYTES:
                    raise HTTPException(413, "SKILL.md must be at most 128 KiB.")
                chunks.append(chunk)
            content = b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(422, "SKILL.md must be UTF-8 text.") from exc
        try:
            return InstructionSkill.model_validate(selected_library.create(content))
        except SkillLibraryError as exc:
            raise _skill_error(exc) from exc

    @app.put(
        "/api/skills/{skill_id}", response_model=InstructionSkill, responses=errors
    )
    def update_skill(
        skill_id: str, request: Request, body: SkillUpdateRequest
    ) -> InstructionSkill:
        selected_library = managed_library(request)
        try:
            return InstructionSkill.model_validate(
                selected_library.update(
                    skill_id,
                    expected_revision=body.expected_revision,
                    content=body.content,
                )
            )
        except SkillLibraryError as exc:
            raise _skill_error(exc) from exc

    @app.patch(
        "/api/skills/{skill_id}/enabled",
        response_model=InstructionSkill,
        responses=errors,
    )
    def set_skill_enabled(
        skill_id: str, request: Request, body: SkillEnabledRequest
    ) -> InstructionSkill:
        selected_library = managed_library(request)
        try:
            return InstructionSkill.model_validate(
                selected_library.set_enabled(
                    skill_id,
                    expected_revision=body.expected_revision,
                    enabled=body.enabled,
                )
            )
        except SkillLibraryError as exc:
            raise _skill_error(exc) from exc

    @app.delete("/api/skills/{skill_id}", status_code=204, responses=errors)
    def delete_skill(
        skill_id: str, request: Request, body: SkillDeleteRequest
    ) -> Response:
        selected_library = managed_library(request)
        try:
            selected_library.delete(skill_id, expected_revision=body.expected_revision)
        except SkillLibraryError as exc:
            raise _skill_error(exc) from exc
        return Response(status_code=204)
