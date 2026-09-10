"""Host-owned param-validator registry (server-truthful preflight).

A param declares a validator by KEY from this host-owned vocabulary
(``ui_hints.param_validators`` on a catalog entry); the server runs the
validator and returns a diagnostic. The browser's JS ``RegExp`` dialect must
never judge a Python pattern, so validation truth lives here.

Ship exactly ONE validator — ``python_regex`` — compiled with the same
``regex`` engine the executor runs (``frisket.actions.extract`` imports
``regex as safe_regex`` and calls ``safe_regex.compile``), so preflight truth
matches run truth: a pattern that passes here is one the row loop can compile,
and named-group syntax like ``(?P<name>...)`` (Python, not JS ``(?<name>...)``)
is accepted here exactly because the executor accepts it.

The registry is the seam: validator #2 is one entry in ``_PARAM_VALIDATORS``
plus a ``param_validators`` declaration key on some action's catalog ui_hints
(a typed action, or a plugin manifest param —
``server/action_catalog_hints.py`` projects the same key for plugins).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import regex as safe_regex

from frisket.authoring.templates import BRACE_TEMPLATE_RE


@dataclass(frozen=True)
class ParamValidationContext:
    """Sheet-scoped facts a validator may consult. ``column_names`` is the set of
    real column names on the action's target sheet, resolved by the service from
    ``params.sheet_id`` — the ``template`` validator judges ``{{placeholder}}``
    tokens against it. ``None`` (the default) means the service could not resolve
    a sheet at all; the validator then passes rather than false-alarming on
    placeholders it cannot judge. A resolved (even empty) set is judged."""

    column_names: frozenset[str] | None = None


@dataclass(frozen=True)
class ParamDiagnostic:
    """One param's server verdict. ``position`` is the 0-based offset into the
    submitted value where the error was detected, when the validator can point
    at it (regex compile errors carry ``.pos``)."""

    ok: bool
    message: str | None = None
    position: int | None = None

    def to_payload(self) -> dict:
        payload: dict = {"ok": self.ok}
        if self.message is not None:
            payload["message"] = self.message
        if self.position is not None:
            payload["position"] = self.position
        return payload


ParamValidator = Callable[[str, ParamValidationContext], ParamDiagnostic]


def _validate_python_regex(
    value: str, _context: ParamValidationContext
) -> ParamDiagnostic:
    # Empty is invalid the same way RegexExtractParams.pattern (min_length=1)
    # rejects it at enqueue: an empty pattern never runs, so preflight says so.
    if not value.strip():
        return ParamDiagnostic(ok=False, message="The regex pattern is empty.")
    try:
        safe_regex.compile(value)
    except safe_regex.error as exc:
        pos = getattr(exc, "pos", None)
        return ParamDiagnostic(
            ok=False,
            message=str(getattr(exc, "msg", None) or exc),
            position=pos if isinstance(pos, int) else None,
        )
    return ParamDiagnostic(ok=True)


def _validate_template(value: str, context: ParamValidationContext) -> ParamDiagnostic:
    # A ``{{column}}`` template is valid text at any content; the only
    # server-truthful failure a preflight can catch is a placeholder that names
    # a column the sheet does not have — the exact case the row loop would
    # render as an empty substitution (frisket.templates.render_column_template
    # returns "" for an unknown name), silently dropping the intended value.
    # We judge canonical ``{{name}}`` tokens only (the chip inserter emits those,
    # matching the executor's canonical syntax); legacy ``@col`` is left to
    # older saved specs. An empty/placeholderless template passes here — the
    # param's own min_length gate owns emptiness.
    columns = context.column_names
    if columns is None:
        return ParamDiagnostic(ok=True)
    for match in BRACE_TEMPLATE_RE.finditer(str(value or "")):
        name = match.group(1).strip()
        if name and name not in columns:
            return ParamDiagnostic(
                ok=False,
                message=f'No column named "{name}" on this sheet.',
                position=match.start(),
            )
    return ParamDiagnostic(ok=True)


_PARAM_VALIDATORS: dict[str, ParamValidator] = {
    "python_regex": _validate_python_regex,
    "template": _validate_template,
}


def validator_keys() -> frozenset[str]:
    return frozenset(_PARAM_VALIDATORS)


def validate_param(
    validator_key: str,
    value: str,
    context: ParamValidationContext | None = None,
) -> ParamDiagnostic:
    validator = _PARAM_VALIDATORS.get(validator_key)
    if validator is None:
        raise KeyError(f"unknown param validator: {validator_key!r}")
    return validator(value, context or ParamValidationContext())
