"""Narrow manager-authorized skill authoring for Project Ask."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Literal
from uuid import uuid4

from frisket.engine.store.project_qa import (
    ProjectQAConflictError,
    ProjectQANotFoundError,
    ProjectQAStore,
)
from frisket.server.services.project_qa_authority import BackgroundAskContext
from frisket.server.services.skills import (
    SkillLibrary,
    SkillLibraryError,
    SkillNotFound,
    SkillRevisionConflict,
)
from frisket.server.thread_worker import await_thread_worker


logger = logging.getLogger(__name__)

SkillScope = Literal["workspace", "organization"]
SkillManagerAuthorizer = Callable[[str, BackgroundAskContext], None]


class ProjectQASkillConflict(ProjectQAConflictError):
    """Typed conflict suitable for the Save editor's recovery UI."""

    def __init__(
        self, code: str, message: str, *, current_revision: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision

    def detail(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.current_revision is not None:
            value["current_revision"] = self.current_revision
        return value


class ProjectQASkillAdapter:
    """Composition-owned access to one existing SkillLibrary instance."""

    def __init__(
        self,
        *,
        library: Callable[[], SkillLibrary],
        authorize: SkillManagerAuthorizer,
        scope: SkillScope,
    ) -> None:
        self._library = library
        self._authorize = authorize
        self.scope = scope

    def can_manage(self, project_id: str, context: BackgroundAskContext) -> bool:
        try:
            self._authorize(project_id, context)
        except (PermissionError, ValueError):
            return False
        return True

    def bind(
        self,
        *,
        store: ProjectQAStore,
        project_id: str,
        thread_id: str,
        turn_id: str,
        context: BackgroundAskContext,
    ) -> BoundProjectQASkillAuthoring:
        self._authorize(project_id, context)
        return BoundProjectQASkillAuthoring(
            adapter=self,
            store=store,
            project_id=project_id,
            thread_id=thread_id,
            turn_id=turn_id,
            context=context,
        )

    def _authorize_bound(
        self, project_id: str, context: BackgroundAskContext
    ) -> SkillLibrary:
        self._authorize(project_id, context)
        return self._library()

    @staticmethod
    def _metadata(skill: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: skill[key]
            for key in ("id", "name", "description", "enabled", "revision")
        }

    def _list(self, project_id: str, context: BackgroundAskContext) -> dict[str, Any]:
        library = self._authorize_bound(project_id, context)
        return {
            "skills": [self._metadata(item) for item in library.list()["skills"]],
            "scope": self.scope,
        }

    def _read(
        self, project_id: str, context: BackgroundAskContext, skill_id: str
    ) -> dict[str, Any]:
        library = self._authorize_bound(project_id, context)
        return {**library.get(skill_id), "scope": self.scope}

    def _propose(
        self,
        *,
        store: ProjectQAStore,
        project_id: str,
        thread_id: str,
        turn_id: str,
        context: BackgroundAskContext,
        content: str,
        target_skill_id: str | None,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        library = self._authorize_bound(project_id, context)
        parsed = library.validate(content)
        turn = store.get_turn(turn_id)
        if turn["thread_id"] != thread_id:
            raise ProjectQANotFoundError("skill proposal turn not found")
        if target_skill_id is None:
            if expected_revision is not None:
                raise ValueError(
                    "New skill proposals do not have an expected revision."
                )
            enabled = True
        else:
            if expected_revision is None:
                raise ValueError("Skill updates require an expected revision.")
            current = library.get(target_skill_id)
            if current["revision"] != expected_revision:
                raise ProjectQASkillConflict(
                    "revision_conflict",
                    "This skill changed. Refresh before drafting an update.",
                    current_revision=int(current["revision"]),
                )
            if parsed["name"] != current["name"]:
                raise SkillLibraryError(
                    "Chat updates must keep the installed skill name. Rename it in Settings."
                )
            enabled = bool(current["enabled"])
        proposal = {
            "proposal_id": f"qa_skill_proposal_{uuid4().hex}",
            "target_skill_id": target_skill_id,
            "base_revision": expected_revision,
            "name": parsed["name"],
            "description": parsed["description"],
            "content": content,
            "enabled": enabled,
            "scope": self.scope,
        }
        event = store.append_skill_proposal(turn_id, payload=proposal)
        return {**proposal, "event_seq": event["seq"]}

    def _saved_result(
        self,
        library: SkillLibrary,
        *,
        attempt: Mapping[str, Any],
        saved: Mapping[str, Any],
    ) -> dict[str, Any]:
        saved_payload = saved["payload"]
        try:
            current = library.get(str(saved_payload["skill_id"]))
        except SkillNotFound:
            current = None
        status = (
            "deleted"
            if current is None
            else "saved"
            if int(current["revision"]) == int(saved_payload["revision"])
            else "changed"
        )
        return {
            "status": status,
            "attempt_id": attempt["payload"]["attempt_id"],
            "proposal_id": saved_payload["proposal_id"],
            "skill": current,
        }

    @staticmethod
    def _include_explicit_research_skill(
        store: ProjectQAStore, thread_id: str, skill: Mapping[str, Any]
    ) -> None:
        if not bool(skill["enabled"]):
            return
        thread = store.get_thread(thread_id)
        research = thread.get("research")
        if not isinstance(research, Mapping) or research.get("skills") is None:
            return
        selected = research.get("skills")
        if not isinstance(selected, list):
            return
        name = str(skill["name"])
        if name in selected:
            return
        store.update_thread(
            thread_id,
            expected_revision=int(thread["revision"]),
            research={**research, "skills": [*selected, name]},
        )

    @staticmethod
    def _reconciled_skill(
        library: SkillLibrary,
        *,
        target_skill_id: str | None,
        name: str,
        content: str,
        enabled: bool,
        expected_revision: int | None,
    ) -> dict[str, Any] | None:
        try:
            current = (
                library.get(target_skill_id)
                if target_skill_id is not None
                else library.find_by_name(name)
            )
        except SkillNotFound:
            return None
        if current is None:
            return None
        resulting_revision = 1 if expected_revision is None else expected_revision + 1
        if (
            current["content"] == content
            and bool(current["enabled"]) is enabled
            and int(current["revision"]) == resulting_revision
        ):
            return current
        return None

    def _save(
        self,
        *,
        store: ProjectQAStore,
        project_id: str,
        thread_id: str,
        turn_id: str,
        context: BackgroundAskContext,
        proposal_id: str,
        request_id: str,
        content: str,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        library = self._authorize_bound(project_id, context)
        turn = store.get_turn(turn_id)
        if turn["thread_id"] != thread_id:
            raise ProjectQANotFoundError("skill proposal turn not found")
        proposal_event = store.skill_proposal(turn_id, proposal_id)
        proposal = proposal_event["payload"]
        parsed = library.validate(content)
        attempt_record = store.skill_save_request(turn_id, proposal_id, request_id)
        if attempt_record is not None:
            recorded = attempt_record["attempt"]["payload"]
            if (
                recorded.get("content") != content
                or recorded.get("expected_revision") != expected_revision
                or recorded.get("name") != parsed["name"]
                or recorded.get("description") != parsed["description"]
            ):
                raise ProjectQASkillConflict(
                    "request_conflict",
                    "request_id was already submitted with different content",
                )
            target_skill_id = recorded.get("target_skill_id")
            enabled = bool(recorded.get("enabled"))
        else:
            latest_saved = store.latest_skill_saved(turn_id, proposal_id)
            target_skill_id = proposal.get("target_skill_id")
            if target_skill_id is None and latest_saved is not None:
                target_skill_id = latest_saved["payload"].get("skill_id")
            if target_skill_id is None:
                if expected_revision is not None:
                    raise ValueError("New skills do not have an expected revision.")
                enabled = True
            else:
                if expected_revision is None:
                    raise ValueError("Skill updates require an expected revision.")
                try:
                    current = library.get(str(target_skill_id))
                except SkillNotFound as exc:
                    raise ProjectQASkillConflict(
                        "revision_conflict", "The installed skill was deleted."
                    ) from exc
                if parsed["name"] != current["name"]:
                    raise SkillLibraryError(
                        "Chat updates must keep the installed skill name. Rename it in Settings."
                    )
                enabled = bool(current["enabled"])
            try:
                attempt_record = store.record_skill_save_attempt(
                    turn_id,
                    proposal_id=proposal_id,
                    request_id=request_id,
                    payload={
                        "target_skill_id": target_skill_id,
                        "expected_revision": expected_revision,
                        "content": content,
                        "name": parsed["name"],
                        "description": parsed["description"],
                        "enabled": enabled,
                    },
                )
            except ProjectQAConflictError as exc:
                raise ProjectQASkillConflict("request_conflict", str(exc)) from exc
        attempt = attempt_record["attempt"]
        if attempt_record["saved"] is not None:
            return self._saved_result(
                library, attempt=attempt, saved=attempt_record["saved"]
            )

        # The attempt is durable before the library mutation, but current
        # manager authority still gates the actual shared write.
        self._authorize(project_id, context)
        skill = self._reconciled_skill(
            library,
            target_skill_id=str(target_skill_id)
            if target_skill_id is not None
            else None,
            name=parsed["name"],
            content=content,
            enabled=enabled,
            expected_revision=expected_revision,
        )
        if skill is None:
            try:
                if target_skill_id is None:
                    skill = library.create(content, enabled=True)
                else:
                    skill = library.update(
                        str(target_skill_id),
                        expected_revision=int(expected_revision),
                        content=content,
                    )
            except SkillRevisionConflict as exc:
                current = library.get(str(target_skill_id))
                raise ProjectQASkillConflict(
                    "revision_conflict",
                    str(exc),
                    current_revision=int(current["revision"]),
                ) from exc
            except SkillLibraryError as exc:
                code = (
                    "name_conflict"
                    if "already exists" in str(exc)
                    else "invalid_content"
                )
                if code == "name_conflict":
                    raise ProjectQASkillConflict(code, str(exc)) from exc
                raise

        saved = store.record_skill_saved(
            turn_id,
            proposal_id=proposal_id,
            attempt_id=str(attempt["payload"]["attempt_id"]),
            payload={
                "skill_id": skill["id"],
                "name": skill["name"],
                "description": skill["description"],
                "revision": skill["revision"],
                "enabled": skill["enabled"],
                "status": "saved",
            },
        )
        try:
            self._include_explicit_research_skill(store, thread_id, skill)
        except ProjectQAConflictError:
            # The library write and its receipt are already committed. A
            # concurrent options edit must not turn a successful Save into an error.
            logger.warning(
                "Skill saved; concurrent chat settings change prevented selection"
            )
        return self._saved_result(library, attempt=attempt, saved=saved)

    async def save(self, **kwargs: Any) -> dict[str, Any]:
        return await await_thread_worker(self._save, **kwargs)


class BoundProjectQASkillAuthoring:
    """Per-turn tool surface containing no client-selectable scope identifiers."""

    def __init__(
        self,
        *,
        adapter: ProjectQASkillAdapter,
        store: ProjectQAStore,
        project_id: str,
        thread_id: str,
        turn_id: str,
        context: BackgroundAskContext,
    ) -> None:
        self._adapter = adapter
        self._store = store
        self._project_id = project_id
        self._thread_id = thread_id
        self._turn_id = turn_id
        self._context = context

    async def list_skills(self) -> dict[str, Any]:
        return await await_thread_worker(
            self._adapter._list, self._project_id, self._context
        )

    async def read_skill(self, skill_id: str) -> dict[str, Any]:
        return await await_thread_worker(
            self._adapter._read, self._project_id, self._context, skill_id
        )

    async def propose_skill(
        self,
        content: str,
        target_skill_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return await await_thread_worker(
            self._adapter._propose,
            store=self._store,
            project_id=self._project_id,
            thread_id=self._thread_id,
            turn_id=self._turn_id,
            context=self._context,
            content=content,
            target_skill_id=target_skill_id,
            expected_revision=expected_revision,
        )
