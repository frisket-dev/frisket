"""Read-only replace-rules evaluation preview (no receipt, no side effects).

The single source of rule semantics for resolve.replace: the same evaluation
helpers this module exposes are consumed by the commit executor, so the live
"test a value" box and per-rule match counts in the authoring UI can never
disagree with what Apply writes (Python ``re`` — never the browser's RegExp).

Rule semantics (shared, do not fork):
- Ordered list, FIRST match wins.
- ``contains`` = substring; ``exact`` = whole-string equality; ``regex`` =
  Python ``re.search``.
- ``case_sensitive`` defaults False → casefold comparison / ``re.IGNORECASE``.
- A match sets the WHOLE cell to the rule's target (``None`` → null cell).
- No match → the ``unmatched`` policy ("keep" the original / "null" the cell).
- MISSING source cells — null, empty, or whitespace-only (the same "missing"
  fact ``column_values`` reports, and fill_missing's default) — are
  NEVER matched and stay null regardless of the ``unmatched`` policy; they are
  excluded from ``rule_counts`` and ``unmatched_rows`` (nothing about them
  changes on Apply; fill_missing is the tool for those).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, Sequence

from pydantic import ValidationError

from frisket.preview.common import (
    ColumnPreviewError,
    require_visible_column,
    require_visible_sheet,
)

if TYPE_CHECKING:
    from frisket.actions.resolve import ReplaceRule

MAX_REPLACE_RULES = 200

REPLACE_RULES_PREVIEW_SCHEMA_VERSION = "frisket.replace_rules_preview.v1"
REPLACE_UNMATCHED_POLICIES = ("keep", "null")


def cell_is_missing(value: Any) -> bool:
    """True for cells the resolve.* transforms treat as MISSING: null, the
    empty string, or a whitespace-only string. This is the single "missing"
    predicate shared by the replace preview and the resolve.replace commit
    executor, and it matches ``column_values``'s ``missing`` fact and
    ``fill_missing``'s default (``treat_blank_as_missing=false`` on
    fill_missing is the ONLY way to treat blanks as values, by design).
    Non-string values are stringified first, mirroring rule evaluation."""
    if value is None:
        return True
    text = value if isinstance(value, str) else str(value)
    return not text.strip()


class ReplaceRulesPreviewError(ColumnPreviewError):
    pass


@dataclass(frozen=True)
class CompiledReplaceRule:
    """One rule prepared for repeated evaluation: the regex is compiled once
    per request (never per cell), and case-insensitive contains/exact rules
    precompute their casefolded needle."""

    index: int
    match: str
    pattern: str
    target: str | None
    case_sensitive: bool
    regex: re.Pattern[str] | None
    needle: str  # pattern, casefolded when case-insensitive contains/exact


class ReplaceRuleLike(Protocol):
    match: str
    pattern: str
    target: str | None
    case_sensitive: bool


def compile_replace_rules(
    rules: Sequence[ReplaceRuleLike],
) -> tuple[CompiledReplaceRule, ...]:
    """Compile validated ``ReplaceRule``s for evaluation. Shared by the preview
    endpoint and the resolve.replace commit executor — the one place rule
    semantics are turned into runnable matchers."""
    compiled: list[CompiledReplaceRule] = []
    for index, rule in enumerate(rules):
        regex: re.Pattern[str] | None = None
        if rule.match == "regex":
            flags = 0 if rule.case_sensitive else re.IGNORECASE
            regex = re.compile(rule.pattern, flags)
        needle = rule.pattern if rule.case_sensitive else rule.pattern.casefold()
        compiled.append(
            CompiledReplaceRule(
                index=index,
                match=rule.match,
                pattern=rule.pattern,
                target=rule.target,
                case_sensitive=rule.case_sensitive,
                regex=regex,
                needle=needle,
            )
        )
    return tuple(compiled)


def evaluate_replace_rules(
    value: Any,
    compiled: Sequence[CompiledReplaceRule],
    unmatched: str = "keep",
) -> tuple[int | None, str | None]:
    """Evaluate one cell value against the ordered compiled rules.

    Returns ``(matched_rule_index, output)``:
    - a MISSING cell (null, empty, or whitespace-only — ``cell_is_missing``)
      → ``(None, None)``: never matched, stays null, the ``unmatched`` policy
      does NOT apply.
    - First matching rule → ``(index, rule.target)`` (target ``None`` = null).
    - No rule matches → ``(None, original text)`` under "keep",
      ``(None, None)`` under "null".
    Non-string values are matched against ``str(value)``.
    """
    if cell_is_missing(value):
        return None, None
    text = value if isinstance(value, str) else str(value)
    for rule in compiled:
        if rule.match == "regex":
            assert rule.regex is not None
            if rule.regex.search(text) is not None:
                return rule.index, rule.target
            continue
        haystack = text if rule.case_sensitive else text.casefold()
        if rule.match == "contains":
            if rule.needle in haystack:
                return rule.index, rule.target
        elif haystack == rule.needle:  # exact
            return rule.index, rule.target
    if unmatched == "null":
        return None, None
    return None, text


def parse_replace_rules(rules: Any) -> list[ReplaceRule]:
    """Validate a raw wire ``rules`` payload through the ReplaceRule contract
    model (the same model the commit params embed), mapping validation failures
    onto the preview error contract: a non-compiling regex → ``invalid_regex``,
    anything else → ``invalid_params``."""
    from frisket.actions.resolve import ReplaceRule

    if not isinstance(rules, list):
        raise ReplaceRulesPreviewError(
            "invalid_params",
            "replace rules preview requires rules to be a list",
            field="rules",
        )
    if len(rules) > MAX_REPLACE_RULES:
        raise ReplaceRulesPreviewError(
            "invalid_params",
            f"replace rules preview accepts at most {MAX_REPLACE_RULES} rules",
            field="rules",
        )
    parsed: list[ReplaceRule] = []
    for index, raw in enumerate(rules):
        if not isinstance(raw, dict):
            raise ReplaceRulesPreviewError(
                "invalid_params",
                "each replace rule must be an object",
                field=f"rules[{index}]",
            )
        try:
            parsed.append(ReplaceRule.model_validate(raw))
        except ValidationError as exc:
            if any(
                "invalid regex" in str(err.get("msg", "")).replace("_", " ")
                for err in exc.errors()
            ):
                raise ReplaceRulesPreviewError(
                    "invalid_regex",
                    "replace rule regex pattern does not compile",
                    field=f"rules[{index}].pattern",
                    details={"pattern": raw.get("pattern")},
                ) from exc
            raise ReplaceRulesPreviewError(
                "invalid_params",
                "replace rule is invalid",
                field=f"rules[{index}]",
            ) from exc
    return parsed


def resolve_replace_rules_preview(
    project: Any,
    *,
    sheet_id: Any,
    input_column: Any,
    rules: Any,
    unmatched: Any = "keep",
    test_value: Any = None,
) -> dict[str, Any]:
    """Evaluate the ordered rules over one column, read-only.

    Returns per-rule match counts over the FULL visible column, the
    ``unmatched_rows`` count, an optional single test-value trace, and the
    current full source values used by the eventual typed commit request. The
    legacy ``value_hash`` remains for compatibility with cluster consumers.
    An empty ``rules`` list is allowed here (the authoring drawer opens empty
    and still wants a snapshot plus the unmatched count); the commit requires
    at least one rule.
    """
    snapshot_factory = getattr(project, "read_snapshot", None)
    if callable(snapshot_factory):
        with snapshot_factory() as snapshot:
            return resolve_replace_rules_preview(
                snapshot,
                sheet_id=sheet_id,
                input_column=input_column,
                rules=rules,
                unmatched=unmatched,
                test_value=test_value,
            )

    if not isinstance(sheet_id, int) or isinstance(sheet_id, bool) or sheet_id < 1:
        raise ReplaceRulesPreviewError(
            "invalid_input_ref",
            "replace rules preview requires a positive sheet_id",
            field="sheet_id",
        )
    if not isinstance(input_column, str) or not input_column.strip():
        raise ReplaceRulesPreviewError(
            "invalid_input_ref",
            "replace rules preview requires a non-empty input_column",
            field="input_column",
        )
    column_name = input_column.strip()
    if unmatched not in REPLACE_UNMATCHED_POLICIES:
        raise ReplaceRulesPreviewError(
            "invalid_params",
            f"unmatched must be one of {REPLACE_UNMATCHED_POLICIES}",
            field="unmatched",
        )
    if test_value is not None and not isinstance(test_value, str):
        raise ReplaceRulesPreviewError(
            "invalid_params",
            "test_value must be a string when sent",
            field="test_value",
        )
    compiled = compile_replace_rules(parse_replace_rules(rules))

    require_visible_sheet(
        project, sheet_id, error=ReplaceRulesPreviewError, label="replace rules preview"
    )
    column = require_visible_column(
        project,
        sheet_id,
        column_name,
        error=ReplaceRulesPreviewError,
        label="replace rules preview",
    )
    column_id = int(column["id"])

    row_ids = [int(row_id) for row_id in project.visible_row_ids(sheet_id)]
    # ONE read of the column: the counts and the value_hash staleness anchor
    # describe the same snapshot (hash_column_values is the helper behind
    # cluster.source_value_hash — the anchor the commit validates).
    values = project.get_values(sheet_id, column_id)
    from frisket.preview.cluster import hash_column_values

    value_hash = hash_column_values(row_ids, values)

    # Matching is deterministic per value, so evaluate each DISTINCT
    # non-missing value once and multiply by its row count. Missing cells
    # (null/blank/whitespace-only) are excluded from rule_counts AND
    # unmatched_rows — they stay null on Apply.
    distinct: Counter[str] = Counter()
    for row_id in row_ids:
        raw = values.get(row_id)
        if cell_is_missing(raw):
            continue
        distinct[raw if isinstance(raw, str) else str(raw)] += 1

    matched_rows = [0] * len(compiled)
    matched_values = [0] * len(compiled)
    unmatched_rows = 0
    for text, count in distinct.items():
        index, _output = evaluate_replace_rules(text, compiled, unmatched)
        if index is None:
            unmatched_rows += count
        else:
            matched_rows[index] += count
            matched_values[index] += 1

    test_result: dict[str, Any] | None = None
    if test_value is not None:
        index, output = evaluate_replace_rules(test_value, compiled, unmatched)
        test_result = {"matched_rule_index": index, "output": output}

    return {
        "schema_version": REPLACE_RULES_PREVIEW_SCHEMA_VERSION,
        "sheet_id": sheet_id,
        "column_id": column_id,
        "total_rows": len(row_ids),
        "rule_counts": [
            {
                "index": index,
                "matched_rows": matched_rows[index],
                "matched_values": matched_values[index],
            }
            for index in range(len(compiled))
        ],
        "unmatched_rows": unmatched_rows,
        "test_result": test_result,
        "value_hash": value_hash,
    }
