"""The output-column retirement mechanism, extracted from
map_runner.py so leaf callers need not import the engine. Operates on an
explicit ``project`` rather than an implicit ``self``.
"""

from __future__ import annotations

import json
from typing import Any

from frisket.ops.base import Recipe
from frisket.engine.store import Project


def spec_action_kind(spec: dict) -> str | None:
    value = spec.get("action_kind")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _norm_output_name(value: Any) -> str | None:
    """Normalize an output-column name for comparison the SAME way emitters
    derive it — case/space-folded (translate lowercases its emitted column,
    ops/builtin.py `_translation_column`; this is the shared fold, not a new
    constant). Non-strings / blanks -> None (unknowable)."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


def _declared_field_names(params: Any) -> set[str]:
    """Multi-output instances named by a persisted runner spec's fields."""
    if not isinstance(params, dict):
        return set()
    fields = params.get("fields")
    if not isinstance(fields, list):
        return set()
    return {
        name
        for name in (
            _norm_output_name(field.get("name"))
            for field in fields
            if isinstance(field, dict)
        )
        if name is not None
    }


def retirement_owns_column(
    project: Project,
    prior_run_id: Any,
    action_kind: str | None,
    output_name: Any,
    name: str,
    current_output_names: set[str],
) -> bool:
    """Whether a retirement candidate is THIS output's to retire.

    The ownership guard is true only for:
      * an unattributed NULL run pointer whose name is OUTPUT-DERIVED — a
        ``{out}_*`` suffix of one of this run's outputs (seeded legacy
        `_confidence`/`_justification`). A shared ABSOLUTE name
        (`detected_language`) with no run pointer is NOT provably anyone's
        and stays visible (F5), or
      * a column a prior run of the SAME action_kind AND the SAME output
        produced (output-INSTANCE attribution — transcribe declares the
        absolute `detected_language`, so action-match alone would let output
        "interview" hide output "transcript"'s detected_language; F2).
    A different action's run, a different output's run, a dangling pointer,
    or an unknowable action/output is never ours to hide. Attribution reads
    the run's canonical action_kind column (post recipe-kind-cutover), never
    the retained-but-unread legacy id column. Names compare case-folded so a
    rerun that re-cases the output still matches its own columns (F7)."""
    low_name = name.strip().lower()
    if prior_run_id is None:
        # NULL-owned ONLY for a name derived from one of this run's outputs.
        return any(
            low_name.startswith(f"{folded}_")
            for folded in (
                _norm_output_name(candidate) for candidate in current_output_names
            )
            if folded is not None
        )
    if action_kind is None:
        return False
    row = project.db.execute(
        "SELECT action_kind, params FROM runs WHERE id=?", (prior_run_id,)
    ).fetchone()
    if row is None or row["action_kind"] != action_kind:
        return False
    try:
        prior_params = json.loads(row["params"] or "{}")
    except (TypeError, ValueError):
        prior_params = {}
    this_output = _norm_output_name(output_name)
    if this_output is not None:
        prior_output = (
            _norm_output_name(prior_params.get("output_name"))
            if isinstance(prior_params, dict)
            else None
        )
        return prior_output == this_output

    # Only genuinely multi-output specs lack one current ``output_name``.
    # Attribute the dropped companion to the longest current output prefix
    # (``topic_similarity_margin`` belongs to ``topic_similarity`` before
    # ``topic``), then require the prior run to declare that exact field.
    # Do not union prior ``output_name`` and ``fields``: when a current
    # single-output name exists, that exact prior name is the authority above.
    current_outputs = sorted(
        {
            name
            for name in map(_norm_output_name, current_output_names)
            if name is not None
        },
        key=len,
        reverse=True,
    )
    this_output = next(
        (name for name in current_outputs if low_name.startswith(f"{name}_")),
        None,
    )
    if this_output is None:
        return False
    return this_output in _declared_field_names(prior_params)


def retire_dropped_output_columns(
    project: Project,
    *,
    op_id: int,
    recipe: Recipe,
    spec: dict,
    columns: list,
    current_output_names: set[str],
    commit: bool = True,
) -> list[int]:
    """The one output-retirement mechanism.

    The RECIPE declares which output-column names a prior configuration of
    this output emitted but the current run no longer does
    (``recipe.retired_output_names(spec)`` — transcribe's detected_language
    when the engine can't detect; clean_column's _confidence/_justification
    after the mechanical cutover). This hook is fully generic: for each
    declared name that still exists as an ai_generated column outside the
    current output set, mark it hide-by-default (values preserved, just out
    of the way; ``set_column_default_hidden`` — the same API the retirement path
    used) and record the retirement on the op's undo_info as the durable
    provenance note. No per-recipe knowledge lives here. Fresh runs only
    (a resume never changes its output set). ``commit=False`` lets an
    atomic-family preparation own the surrounding savepoint.
    """
    candidates = set(recipe.retired_output_names(spec)) - current_output_names
    if not candidates:
        return []
    output_name = spec.get("output_name")
    # This run's canonical action kind, resolved exactly as start_run stamps
    # it (spec's action_kind, else the recipe's canonical kind) so the
    # ownership match reads the same identity that was persisted.
    this_action_kind = spec_action_kind(spec)
    if not this_action_kind:
        raise ValueError("runner spec requires a canonical action_kind")
    retired: list[dict] = []
    for column in columns:
        if (
            column["name"] in candidates
            and column["ai_generated"]
            and not column["default_hidden"]
            # OWNERSHIP: retire a declared name ONLY when the column is
            # this OUTPUT's to retire — an unattributed (NULL run) legacy
            # leftover, or a column a PRIOR run of THIS recipe AND output
            # produced. A column whose run belongs to a different action
            # (classify's score_confidence that clean_column overwrote
            # its `score` onto) or a different output of the same action
            # (output "interview"'s vs "transcript"'s detected_language)
            # is NEVER hidden.
            and retirement_owns_column(
                project,
                column["current_run_id"],
                this_action_kind,
                output_name,
                column["name"],
                current_output_names,
            )
        ):
            project.set_column_default_hidden(int(column["id"]), True, commit=commit)
            retired.append(
                {
                    "column_id": int(column["id"]),
                    "name": column["name"],
                    "reason": "output_role_retired",
                }
            )
    if not retired:
        return []
    op = project.db.execute("SELECT undo_info FROM ops WHERE id=?", (op_id,)).fetchone()
    info = json.loads(op["undo_info"] or "{}") if op is not None else {}
    info["retired_output_columns"] = retired
    project.db.execute(
        "UPDATE ops SET undo_info=? WHERE id=?", (json.dumps(info), op_id)
    )
    if commit:
        project.db.commit()
    return [entry["column_id"] for entry in retired]


def hide_zero_success_created_columns(
    project: Project,
    op_id: int,
    out_cols: dict[str, int],
    *,
    only_columns: set[int] | None = None,
) -> set[int]:
    """Hide the subset of ``out_cols`` this op created fresh (per
    ``ops.undo_info.created_columns``), leaving reused/overwritten
    columns untouched. Caller owns the transaction/commit. The column
    stays pointed at the run (``point_column_at_run`` already ran) — only
    its visibility changes, so receipt/replay lookups that key off
    ``current_run_id`` are unaffected. Returns the hidden column ids.

    Also stamps ``undo_info.zero_success_columns`` with the hidden ids:
    ``Project._reapply`` reads that marker to keep a redo of THIS op from
    resurrecting a column it never actually populated (redo's default
    behavior — unhide every ``created_columns`` entry — assumes the op's
    apply left its columns visible, which isn't true here). ``_unapply``
    needs no matching change: hiding an already-hidden column on undo is
    already a no-op."""
    op = project.db.execute("SELECT undo_info FROM ops WHERE id=?", (op_id,)).fetchone()
    if op is None:
        return set()
    info = json.loads(op["undo_info"] or "{}")
    created = {int(c) for c in info.get("created_columns", [])}
    to_hide = created & set(out_cols.values())
    if only_columns is not None:
        to_hide &= only_columns
    if not to_hide:
        return set()
    for cid in to_hide:
        project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (cid,))
    zero_success = set(info.get("zero_success_columns", [])) | to_hide
    info["zero_success_columns"] = sorted(zero_success)
    project.db.execute(
        "UPDATE ops SET undo_info=? WHERE id=?", (json.dumps(info), op_id)
    )
    return to_hide


def reveal_recovered_columns(
    project: Project,
    op_id: int,
    out_cols: dict[str, int],
    *,
    still_zero_success: set[int] | None = None,
) -> None:
    """The inverse of ``hide_zero_success_created_columns``: a run this
    op previously zero-success-hid can be retargeted later (``run.
    backfill`` explicitly resolves past ``hidden`` and re-runs exactly
    the failed rows via ``resume_run_id``) — if it now produces at least
    one success, un-hide the column and drop it from ``undo_info.
    zero_success_columns`` so it goes back to behaving like an ordinary
    created column (visible now, and on a later redo). A no-op unless
    this op actually hid one of ``out_cols`` before."""
    op = project.db.execute("SELECT undo_info FROM ops WHERE id=?", (op_id,)).fetchone()
    if op is None:
        return
    info = json.loads(op["undo_info"] or "{}")
    zero_success = set(info.get("zero_success_columns", []))
    to_reveal = zero_success & set(out_cols.values())
    if still_zero_success is not None:
        to_reveal -= still_zero_success
    if not to_reveal:
        return
    for cid in to_reveal:
        project.db.execute("UPDATE columns SET hidden=0 WHERE id=?", (cid,))
    info["zero_success_columns"] = sorted(zero_success - to_reveal)
    project.db.execute(
        "UPDATE ops SET undo_info=? WHERE id=?", (json.dumps(info), op_id)
    )
