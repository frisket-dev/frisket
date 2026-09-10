"""Typed LLM-as-judge action on the shared ``ModelRows`` execution path."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, StrictStr, field_validator

from frisket.actions.core import (
    ActionCategory,
    ModelRowsEvaluation,
    ModelRowsEvaluationContext,
    action,
    model_rows,
)
from frisket.actions.model_rows import RichSource
from frisket.actions.types import (
    ActionParams,
    GeneratedColumnRef,
    ModelPrompt,
    ModelRef,
    Row,
)
from frisket.ai.message_content import render_input_block


class JudgeParams(ActionParams):
    source: RichSource = Field(title="Source columns")
    judged_column: GeneratedColumnRef[Any] = Field(title="Answer to grade")
    model: ModelRef
    guidelines: StrictStr = Field(
        title="Guidelines",
        description="Describe what a correct answer must satisfy.",
    )
    include_original_prompt: bool = Field(
        default=False,
        title="Include the original prompt",
        description="Give the judge the instruction that produced the answer.",
    )

    @field_validator("guidelines")
    @classmethod
    def _nonblank_guidelines(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("guidelines must not be blank")
        return stripped


class JudgeOutput(BaseModel):
    verdict: bool = Field(
        description="true if the output is correct per the guidelines"
    )
    judge_note: str = Field(
        description="one-sentence reason; name the failure mode if false"
    )


def judge(
    params: JudgeParams,
    row: Row,
    context: ModelRowsEvaluationContext,
) -> ModelPrompt[JudgeOutput]:
    system = (
        "You are a skeptical evaluator. Given source material and a value under "
        "review, judge ONLY whether the value meets the guidelines. Default to "
        "false when uncertain."
    )
    source_values = {
        name: value
        for name, value in row.values.items()
        if name != params.judged_column.name
    }
    if isinstance(params.source, list):
        source_parts = render_input_block(source_values)
    else:
        source_parts = render_input_block({"input": params.source.render(row)})
        for name, value in source_values.items():
            if isinstance(value, dict) and value.get("__image_b64__"):
                source_parts.extend(render_input_block({name: value}))
    user_parts = [
        {"type": "text", "text": "Source material:"},
        *source_parts,
        {"type": "text", "text": "Answer under review:"},
        *render_input_block(
            {params.judged_column.name: params.judged_column.read(row)}
        ),
    ]
    user_parts.append(
        {
            "type": "text",
            "text": (
                f"\nThe column under review is '{params.judged_column.name}'."
                f"\nGuidelines:\n{params.guidelines}"
            ),
        }
    )
    if context.original_prompt:
        user_parts.append(
            {
                "type": "text",
                "text": (
                    "\nFor context, the value under review was produced by another "
                    "step with this instruction:\n"
                    f"<original_prompt>\n{context.original_prompt}\n</original_prompt>"
                ),
            }
        )
    return ModelPrompt(
        messages=(
            {"role": "system", "content": system},
            {"role": "user", "content": user_parts},
        )
    )


JUDGE = action(
    examples=(
        JudgeParams(
            source=["source"],
            judged_column="answer",
            model="openai/gpt-5-mini",
            guidelines="Every claim must be supported by the source.",
        ),
    ),
    name="judge",
    title="Judge a column against guidelines",
    description=(
        "Grade an AI-generated column's values against your guidelines with a "
        "second model, writing verdict and judge-note columns plus receipt evidence "
        "for the guidelines, output roles, and model calls. The judged column is "
        "included automatically so the model sees the value under review; set "
        "include_original_prompt to add the upstream instruction that produced an "
        "AI-generated judged column."
    ),
    category=ActionCategory.TEXT,
    run=model_rows(
        judge,
        evaluation=ModelRowsEvaluation(
            subject_param="judged_column",
            guidelines_param="guidelines",
            upstream_prompt_param="include_original_prompt",
        ),
    ),
)
