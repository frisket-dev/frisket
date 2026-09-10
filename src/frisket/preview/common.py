"""Shared, verbatim-duplicated pieces of the read-only compare-preview
services (``ocr.py``, ``transcribe.py``, ``translate.py``, and
``topic_segmentation.py``).

Scope note: this module deliberately does NOT unify the per-engine
try/except/``perf_counter`` accumulation loop each service owns. A full read of
all four services confirms the loops share only a silhouette —
cost, error shaping, iteration unit, and result cardinality diverge on ~12
axes, so a generic runner would need an ~8-callback bag (the kind of
speculative one-size "god runner" this project avoids) and net-ADD lines.
Only the byte-identical *pieces*
live here: the ``*ComparePreviewError`` class, the
string helpers (``canonical_engine``, ``str_or_none``), and the
table-parameterized ``engine_descriptor`` that ``ocr.py`` and
``transcribe.py`` emitted verbatim modulo their engine table.
"""

from __future__ import annotations

from typing import Any, Mapping

from frisket.contracts.actions.schemas._engines import EngineDeclaration, find_engine


class ComparePreviewError(Exception):
    """Validation/runtime error that should be returned as a preview 400.

    Each compare-preview module keeps its OWN subclass name
    (``OcrComparePreviewError`` / ``TranscribeComparePreviewError`` /
    ``TranslateComparePreviewError``) so existing ``except <Name>Error`` /
    ``isinstance`` call sites (``server/services/previews.py`` catches each
    by its specific name, one per route) keep working unchanged — only the
    constructor body they all repeated verbatim is shared here.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.details = details or {}


class BillablePreviewDispatch(RuntimeError):
    """A compare/preview code path reached the point of dispatching an engine
    that bills real money.

    This is an INVARIANT VIOLATION, never a user-input error: every compare
    entry point refuses billable engines while coercing the request (a 400
    naming the cost-gated action to run instead), so by the time a dispatch
    helper runs, no billable engine can remain. The fence lives at the effect
    site anyway — the place the provider call actually happens — because a
    validation that lives only in the caller is the optional-wiring bug class
    (a new caller forgets it and the money leaks silently).

    The per-engine ``except Exception`` loops each compare service owns must
    NOT swallow this into a 200-with-per-engine-error: it means the refusal
    upstream has a hole, and it must surface loudly.
    """


def billable_engine_error(
    engine: str,
    *,
    error: type[ComparePreviewError],
    action_kind: str,
    field: str = "engines",
) -> ComparePreviewError:
    """The refusal every compare service raises for a billable engine.

    The predicate is the whole rule: **billable → run, not billable →
    preview.** A comparison that spends money at a provider endpoint already
    IS a run; compare simply never recorded it as one. So compare refuses it
    and names the cost-gated action that does record it — a run, a consent
    record, a receipt, an egress record and a spend line.

    The copy mirrors the wording ``datalab`` already carried as its
    ``not_comparable`` reason (``_engines.py``), which was this same ruling
    applied to one engine by hand.
    """
    return error(
        "billable_engine_requires_run",
        f"'{engine}' bills real money per call; compare is the free surface. "
        f"Run it through the cost-gated {action_kind} action instead, which "
        "records a run, a consent record and a receipt.",
        field=field,
        details={"engine": engine, "action": action_kind},
    )


class ColumnPreviewError(ValueError):
    """Validation/read error a whole-column store-reader preview raises as a
    preview 400.

    Same envelope as ``ComparePreviewError`` (code/message/field/details) but
    ``ValueError``-based, matching the store readers' prior classes. Each
    reader keeps its OWN subclass name (``ClusterPreviewError`` /
    ``ColumnValuesPreviewError`` / ``ReplaceRulesPreviewError``) so
    ``except <Name>Error`` call sites keep working unchanged — only the
    constructor body they repeated verbatim is shared here.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.details = dict(details or {})


def require_visible_sheet(
    project: Any, sheet_id: int, *, error: type[ColumnPreviewError], label: str
) -> Any:
    """The sheet row for a visible ``sheet_id``, or the calling reader's own
    ``invalid_input_ref`` error (``label`` prefixes the message so each
    reader's wording is unchanged)."""
    sheet = project.db.execute(
        "SELECT * FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if sheet is None:
        raise error(
            "invalid_input_ref",
            f"{label} sheet_id does not identify a visible sheet",
            field="sheet_id",
            details={"sheet_id": sheet_id},
        )
    return sheet


def require_visible_column(
    project: Any,
    sheet_id: int,
    column: str,
    *,
    error: type[ColumnPreviewError],
    label: str,
) -> Any:
    """The column row for a visible ``column`` on ``sheet_id``, or the calling
    reader's own ``invalid_input_ref`` error."""
    row = project.db.execute(
        "SELECT * FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
        (sheet_id, column),
    ).fetchone()
    if row is None:
        raise error(
            "invalid_input_ref",
            f"{label} column does not identify a visible column",
            field="input_column",
            details={"sheet_id": sheet_id, "column": column},
        )
    return row


def canonical_engine(engine: str, aliases: Mapping[str, str]) -> str:
    """The engine id after resolving ``aliases`` (each caller's own
    ``ENGINE_ALIASES`` table — OCR's and transcribe's differ, so this takes
    the table as a parameter rather than importing either)."""
    return aliases.get(engine, engine)


def engine_descriptor(
    table: tuple[EngineDeclaration, ...], engine: str
) -> dict[str, Any]:
    """The per-engine descriptor ``ocr`` and ``transcribe``
    emit verbatim (only the table differed): the declaration's fields when the
    engine is known, else an unknown id treated as billable hosted-tier."""
    decl = find_engine(table, engine)
    if decl is not None:
        return {
            "id": decl.id,
            "label": decl.label,
            "tier": decl.tier,
            "billable": decl.billable,
        }
    return {
        "id": engine,
        "label": engine,
        "tier": "hosted",
        "billable": True,
    }


def str_or_none(value: Any, *, strip: bool = True) -> str | None:
    """``value`` as a non-empty string, or ``None``.

    ``strip=True`` (the default, matching ``transcribe.py``'s prior
    behavior) also treats a whitespace-only string as empty and returns the
    stripped value. ``ocr.py``'s prior behavior did not strip —
    pass ``strip=False`` there to preserve it exactly."""
    if not isinstance(value, str):
        return None
    if strip:
        stripped = value.strip()
        return stripped or None
    return value or None
