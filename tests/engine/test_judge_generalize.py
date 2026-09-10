"""Judge contract: AI-generated target, same-family allowed, generic skeleton."""

from __future__ import annotations

import json
import copy
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.store import Project

PROJECT_ID = "project-judge-generalize"
WORKFLOW_ID = "judge-generalize"


class _SequenceAdapter:
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        index = min(len(self.requests), len(self.replies) - 1)
        reply = dict(self.replies[index])
        self.requests.append(req)
        return LLMResponse(
            content=json.dumps(reply),
            data=reply,
            tokens_in=71,
            tokens_out=19,
            cost=0.005,
            model=req.model,
        )


def _stub_router(*replies: dict[str, Any]) -> tuple[ModelRouter, _SequenceAdapter]:
    router = ModelRouter(
        keys={"anthropic": "k", "openai": "k"}, cache=None, cache_mode="off"
    )
    adapter = _SequenceAdapter(list(replies))
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    router._adapters["openai"] = adapter  # noqa: SLF001
    return router, adapter


def _seed_project(project_path: Path) -> int:
    project = Project.create(project_path, name="Judge Generalize")
    try:
        sheet_id = project.add_sheet("Sheet")
        columns = {
            "story": project.add_column(sheet_id, "story", type="text"),
            "source_url": project.add_column(sheet_id, "source_url", type="link"),
        }
        project.add_rows(
            sheet_id,
            [
                {
                    "story": "City hall awarded a no-bid contract.",
                    "source_url": "https://example.com/contract",
                },
                {
                    "story": "Road repairs finished under budget.",
                    "source_url": "https://example.com/road",
                },
            ],
            columns,
        )
        return sheet_id
    finally:
        project.close()


# The upstream instruction text the original-prompt trace must surface.
_CLASSIFY_CONTEXT = "Score accountability risk for the judge-generalize oracle."


def _classify_action(sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["story"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "context": _CLASSIFY_CONTEXT,
            "fields": [
                {
                    "name": "risk_score",
                    "type": "score",
                    "description": "Public accountability risk from 0 to 10.",
                }
            ],
        },
        "idempotency_key": "judge_generalize_classify@sha256:stable",
    }


def _judge_action(
    sheet_id: int,
    *,
    judged_column: str = "risk_score",
    input_columns: list[str] | str | None = None,
    model: str = "openai/gpt-5-mini",
    include_original_prompt: bool | None = None,
    idempotency_key: str = "judge_generalize@sha256:stable",
) -> dict[str, Any]:
    selected_source = input_columns or ["story", judged_column]
    source = (
        {"text": selected_source}
        if isinstance(selected_source, str)
        else [name for name in selected_source if name != judged_column]
    )
    params: dict[str, Any] = {
        "source": source,
        "judged_column": judged_column,
        "model": model,
        "guidelines": "The value must be supported by concrete facts in the story.",
    }
    if include_original_prompt is not None:
        params["include_original_prompt"] = include_original_prompt
    return {
        "action_id": "map.judge",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": params,
        "idempotency_key": idempotency_key,
    }


def _run(
    project: Project, action: dict[str, Any], router: ModelRouter, step: str
) -> Any:
    del step
    from frisket.engine.executor import run_action_spec

    challenge = run_action_spec(project, action, project_id=PROJECT_ID, router=router)
    if challenge.status != "needs_confirmation":
        return challenge
    retry = copy.deepcopy(action)
    retry["confirmation"] = challenge.errors[0].details["promise_set_hash"]
    return run_action_spec(project, retry, project_id=PROJECT_ID, router=router)


def _request_text(req: LLMRequest) -> str:
    return json.dumps(req.messages)


def test_typed_upstream_prompt_uses_only_nested_author_params() -> None:
    from frisket.engine.executor.map_rows_action import _format_upstream_prompt

    for action_kind, author_params, expected_text in (
        (
            "ask",
            {
                "source": ["story"],
                "model": "openai/secret-routing-model",
                "context": "City reporting",
                "question": "What happened?",
            },
            "What happened?",
        ),
        (
            "summarize",
            {
                "source": ["story"],
                "model": "anthropic/secret-routing-model",
                "preset": "one_line",
                "instruction": "Keep the date.",
            },
            "Keep the date.",
        ),
    ):
        formatted = _format_upstream_prompt(
            json.dumps(
                {
                    "action_kind": action_kind,
                    "action_version": "1",
                    "input_columns": ["story"],
                    "output_names": {"answer": "generated"},
                    "overwrite": True,
                    "params": author_params,
                }
            )
        )
        assert formatted is not None
        assert expected_text in formatted
        assert "secret-routing-model" not in formatted
        assert "action_kind" not in formatted
        assert "action_version" not in formatted
        assert "input_columns" not in formatted
        assert "output_names" not in formatted
        assert "overwrite" not in formatted


def test_typed_judge_upstream_prompt_never_recurses_prior_evaluation_context() -> None:
    from frisket.engine.executor.map_rows_action import _format_upstream_prompt

    formatted = _format_upstream_prompt(
        json.dumps(
            {
                "action_kind": "map.judge",
                "action_version": "1",
                "model": "openai/outer-model",
                "params": {
                    "source": ["story"],
                    "judged_column": "answer",
                    "model": "openai/inner-model",
                    "guidelines": "prior private evaluation rubric",
                    "include_original_prompt": True,
                },
                "evaluation_context": {
                    "original_prompt": "prior private producer prompt",
                    "source_run_id": 41,
                },
            }
        )
    )

    assert formatted is not None
    assert "prior private producer prompt" not in formatted
    assert "prior private evaluation rubric" not in formatted
    assert "outer-model" not in formatted
    assert "inner-model" not in formatted


def test_judge_rejects_non_generated_answer_before_model_work(tmp_path: Path) -> None:
    project_path = tmp_path / "non-generated.frisket"
    sheet_id = _seed_project(project_path)
    router, adapter = _stub_router(
        {"verdict": True, "judge_note": "Supported by the story."},
        {"verdict": False, "judge_note": "Not supported."},
    )
    project = Project(project_path)
    try:
        result = _run(
            project,
            _judge_action(
                sheet_id,
                judged_column="source_url",
                input_columns=["story", "source_url"],
                include_original_prompt=True,
                idempotency_key="judge_generalize@sha256:non-generated",
            ),
            router,
            "judge_source_url",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "column_not_ai_generated"
        assert result.errors[0].field == "params.judged_column"
        assert result.errors[0].message == (
            "map.judge answer to grade must be an AI-generated column"
        )
        assert result.errors[0].details == {"column": "source_url"}
        assert adapter.requests == []
    finally:
        project.close()


def test_judge_canonicalizes_omitted_judged_column_into_sources(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "canonical-source-union.frisket"
    sheet_id = _seed_project(project_path)
    router, adapter = _stub_router(
        {"risk_score": 8},
        {"risk_score": 3},
        {"verdict": True, "judge_note": "Supported by the story."},
        {"verdict": True, "judge_note": "Supported by the story."},
    )
    project = Project(project_path)
    try:
        classify = _run(project, _classify_action(sheet_id), router, "classify")
        assert classify.status == "completed", classify.errors
        action = _judge_action(
            sheet_id,
            input_columns=["story"],
            idempotency_key="judge_generalize@sha256:canonical-source-union",
        )
        judge = _run(project, action, router, "judge_canonical_source_union")
        assert judge.status == "completed", judge.errors
        assert "risk_score" in _request_text(adapter.requests[-1])
    finally:
        project.close()


def test_judge_allows_same_model_family(tmp_path: Path) -> None:
    """The judge model may share a provider family with the judged run (soft default)."""
    project_path = tmp_path / "same-family.frisket"
    sheet_id = _seed_project(project_path)
    router, _adapter = _stub_router(
        {"risk_score": 8},
        {"risk_score": 3},
        {"verdict": True, "judge_note": "Plausible."},
        {"verdict": True, "judge_note": "Plausible."},
    )
    project = Project(project_path)
    try:
        classify = _run(project, _classify_action(sheet_id), router, "classify")
        assert classify.status == "completed", classify.errors
        judge = _run(
            project,
            _judge_action(
                sheet_id,
                judged_column="risk_score",
                model="anthropic/claude-sonnet-4-6",  # same family as the haiku judged run
                idempotency_key="judge_generalize@sha256:same-family",
            ),
            router,
            "judge_same_family",
        )
        assert judge.status == "completed", judge.errors
        assert all(e.code != "invalid_judge_model_family" for e in (judge.errors or []))
    finally:
        project.close()


def test_include_original_prompt_injects_for_generated_column(tmp_path: Path) -> None:
    """include_original_prompt=True surfaces the upstream instruction, XML-wrapped."""
    project_path = tmp_path / "include-prompt.frisket"
    sheet_id = _seed_project(project_path)
    router, adapter = _stub_router(
        {"risk_score": 8},
        {"risk_score": 3},
        {"verdict": True, "judge_note": "x"},
        {"verdict": True, "judge_note": "y"},
    )
    project = Project(project_path)
    try:
        classify = _run(project, _classify_action(sheet_id), router, "classify")
        assert classify.status == "completed", classify.errors
        judge = _run(
            project,
            _judge_action(
                sheet_id,
                judged_column="risk_score",
                include_original_prompt=True,
                idempotency_key="judge_generalize@sha256:include-prompt",
            ),
            router,
            "judge_include_prompt",
        )
        assert judge.status == "completed", judge.errors
        # The last requests are the judge's; the upstream instruction must appear,
        # wrapped in an <original_prompt> block.
        judge_text = _request_text(adapter.requests[-1])
        assert "<original_prompt>" in judge_text
        assert _CLASSIFY_CONTEXT in judge_text
    finally:
        project.close()


@pytest.mark.parametrize("subject", ["risk_score", "input"])
def test_judge_template_source_keeps_subject_value_in_prompt(
    tmp_path: Path, subject: str
) -> None:
    project_path = tmp_path / "template-source.frisket"
    sheet_id = _seed_project(project_path)
    router, adapter = _stub_router(
        {"risk_score": 8},
        {"risk_score": 3},
        {"verdict": True, "judge_note": "x"},
        {"verdict": True, "judge_note": "y"},
    )
    project = Project(project_path)
    try:
        classify = _run(project, _classify_action(sheet_id), router, "classify")
        assert classify.status == "completed", classify.errors
        project.db.execute(
            "UPDATE columns SET name=? WHERE sheet_id=? AND name='risk_score'",
            (subject, sheet_id),
        )
        project.db.commit()
        judged = _run(
            project,
            _judge_action(
                sheet_id,
                judged_column=subject,
                input_columns="Source: {{story}}",
                idempotency_key="judge_generalize@sha256:template-source",
            ),
            router,
            "judge_template_source",
        )
        assert judged.status == "completed", judged.errors
        rendered = _request_text(adapter.requests[-1])
        assert "Source: Road repairs finished under budget." in rendered
        assert f"{subject}: 3" in rendered
    finally:
        project.close()
