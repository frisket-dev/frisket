"""Route promises: compiled predicate rows, versioned tables, and three-valued
evaluation.

Graduated from the generic-predicate-list spike with the ``unbounded`` op,
basis-keyed ratchet/coverage, versioned
throughput tables, and the canonical hash contract.

Design record (carried from the spike, still binding):

D1. ORDER/THROUGHPUT TABLE BINDING — pin-by-reference, versioned. The
    evaluator never knows that "egress_class" means anything; a
    ``satisfies_order`` row carries an explicit ``order_ref`` (e.g.
    ``"egress.v1"``) and a hardware-sensitive cost basis carries an explicit
    ``throughput_ref``. The code holds an APPEND-ONLY registry;
    published versions are immutable — a semantic change is a NEW version,
    never an edit (otherwise editing code tables would silently rewrite the
    meaning of recorded promises: a mutation of recorded truth). A reference
    the running code cannot resolve is ``unevaluable``, never a crash, never
    a pass. Referenced versions are pinned by the current-row corpus
    (``tests/execution/route_corpus/``).

D2. COST-BASIS COMPATIBILITY — a pricing-key mismatch is UNEVALUABLE, not
    violated: a candidate priced under a different key gives no evidence the
    bound is exceeded, while still recording the true state.
    ``hardware_class`` differs: under the SAME pricing key
    the quantity re-projects through the pinned throughput table and can
    genuinely exceed the bound -> violated, recorded as diagnostic evidence,
    and refused by admission.
    ``throughput_ref: null`` together with no ``hardware_class`` is the
    explicit *hardware-invariant pricing* sentinel, not ambiguity.

D3. CURRENT-ROW POSTURE — rows with the current six-key schema preserve
    unknown ops, fields, and future extra keys. Retired keys and incomplete
    rows refuse at decode; the evaluator records unknown vocabulary as
    ``unevaluable``. Authoring (``Promise.make``) remains strict.

Hash contract: every
content hash in the execution seam is produced by ONE implementation —
:func:`content_hash` — parameterized by a declared, versioned hash FAMILY
(:data:`HASH_FAMILIES`). A family declares its domain string, its string
normalization (NFC), its float policy, and its output format; the corpus
(``tests/execution/route_corpus/hash_families_gen1.json``) pins a sample
digest per family forever. Every family currently shares the strict rules —
sorted keys, compact separators, NFC-normalized strings recursively, NO
floats in hashed material (quantities are strings/ints — callers with
known-numeric inputs convert them via :func:`decimal_string` BEFORE
hashing), bare-hex sha256 over ``<domain>:<canonical json>``. Display-only
fields are excluded from hashed material by this schema's own field lists
(``HASHED_PROMISE_KEYS``, ``FINGERPRINT_KEYS``).

Identity note: the ``frisket.promise_set.v1``
document shape is the STORE/WIRE shape — the list of ``HASHED_PROMISE_KEYS``
row projections, in row order (NOT a ``{"promises": [...]}`` wrapper) — and
the promise fingerprint domain is ``frisket.promise_row.v1``. There is
exactly one construction for each; ``PromiseSet.set_hash``,
``consented_set_hash``, and the store column all produce the same digest.

Pure stdlib by contract: this is recorded-fact machinery (matches
``targets.py``'s dataclass style; no pydantic).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from frisket.execution.commercial import LIVE_COST_TERM_KEYS

# ---------------------------------------------------------------------------
# Vocabulary (closed for THIS writer; unknown values survive decode, D3)
# ---------------------------------------------------------------------------

KNOWN_OPS = ("eq", "satisfies_order", "le", "unbounded")
AUDIENCES = ("user_claim", "system_promise")
SATISFIED = "satisfied"
VIOLATED = "violated"
UNEVALUABLE = "unevaluable"

# Closed field registry: promises compile ONLY over
# config-phase resolution facts — never observation-only facts
# (revision/device/dtype), so run-start satisfaction is always evaluable.
# Maps field -> the ops this writer may author over it.
#
# ``credential_source`` is intentionally absent. As a run-start
# promise it was tautological where evaluable (on a zero-tariff transport both
# sides read the same route row) and unevaluable where it mattered (on a
# tariffed transport the actual provenance does not exist until the model
# call). It is replaced, not deleted, by the pre-effect USE constraint in
# ``frisket.execution.credential_use``: the consent names the credential
# CLASS and the adapter refuses before the call if its selected credential is
# not of that class. Retired credential-promise rows are not part of the
# current schema and refuse at decode.
PROMISE_FIELDS: Mapping[str, tuple[str, ...]] = {
    "operator": ("eq",),
    "egress_class": ("eq", "satisfies_order"),
    "region": ("eq",),
    "cost": ("le", "unbounded"),
}

# The schema's own hashed-field lists (§1.1): anything outside these lists
# (decoded extras, display-only fields) is excluded from hashed material.
HASHED_PROMISE_KEYS = (
    "field",
    "op",
    "value",
    "basis",
    "order_ref",
    "audience",
)
# Promise fingerprint: the full promise row identity —
# field+op+value+basis+order_ref — not just field.
FINGERPRINT_KEYS = ("field", "op", "value", "basis", "order_ref")

DOMAIN_PROMISE_SET = "frisket.promise_set.v1"
DOMAIN_PROMISE_ROW = "frisket.promise_row.v1"
DOMAIN_BASIS_IDENTITY = "frisket.basis_identity.v1"
DOMAIN_ROUTE_FACTS = "frisket.route_facts.v1"
DOMAIN_EPOCH_PROVENANCE = "frisket.epoch_provenance.v1"
DOMAIN_ROUTE_VIOLATION = "frisket.route_violation.v1"
DOMAIN_ACTION_IDENTITY = "frisket.action_identity.v1"
DOMAIN_STANDING_POLICY = "frisket.standing_policy.v1"
# Test-only family (see its HASH_FAMILIES entry): declared here because the
# registry is the ONE place a domain becomes legal, and a test domain that
# lived outside it would be exactly the silent minting this registry closed.
DOMAIN_TEST = "frisket.test.v1"

EGRESS_ORDER_REF = "egress.v1"


# ---------------------------------------------------------------------------
# Canonical JSON + content hashes — the hash-family registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HashFamily:
    """One versioned hash family: a domain plus its declared rules.

    ``content_hash`` is the ONE implementation, parameterized by these
    declarations; the corpus pins a sample digest per family. A rules change
    is a NEW family (``*.v2``), never an edit — editing would silently
    rewrite recorded identity.
    """

    domain: str
    nfc_strings: bool = True
    # "reject": floats raise. Callers with legitimately numeric inputs
    # (action-identity spec fields, cost quantities) convert the KNOWN
    # numeric fields to canonical decimal strings via ``decimal_string``
    # BEFORE hashing; any float that survives to the hasher is a defect.
    float_policy: str = "reject"
    output: str = "hex"  # bare-hex sha256; never a "sha256:" prefix
    note: str = ""


HASH_FAMILIES: Mapping[str, HashFamily] = {
    family.domain: family
    for family in (
        HashFamily(
            DOMAIN_PROMISE_SET,
            note="list of HASHED_PROMISE_KEYS row projections, in row order",
        ),
        HashFamily(
            DOMAIN_PROMISE_ROW,
            note="FINGERPRINT_KEYS projection of one promise row",
        ),
        HashFamily(
            DOMAIN_BASIS_IDENTITY,
            note="(pricing_key, quantity_unit, order_ref, throughput_ref)",
        ),
        HashFamily(
            DOMAIN_ROUTE_FACTS,
            note="the resolved route fact columns (store layer)",
        ),
        HashFamily(
            DOMAIN_EPOCH_PROVENANCE,
            note="typed observed-provenance shape (store layer)",
        ),
        HashFamily(
            DOMAIN_ROUTE_VIOLATION,
            note="(promise_fingerprint, observed hash) dedupe key",
        ),
        HashFamily(
            DOMAIN_ACTION_IDENTITY,
            note=(
                "construction rules unchanged but material changed: "
                "the runner spec PROJECTED onto its op declaration's "
                "runner-spec shape — a closed-world allowlist — minus "
                "confirmed/consented_promise_set_hash (retry-flow flags), "
                "row_ids (execution scope, not intent). The projection "
                "carries the declaration's canonical action_kind exactly "
                "once; recipe-shaped specs are refused before hashing. Was an open-world "
                "hash of the whole dict minus a two-key denylist, which made "
                "identity depend on WHICH copy of the spec a caller held "
                "(halt markers in runs.params, injected backfill row_ids) — "
                "two probed criticals. Defaults are NOT materialized (absent "
                "stays absent: supplied-vs-absent is semantic at the "
                "ability checker); known-numeric fields are "
                "decimal_string-converted by the caller first. NFC required: "
                "NFD/NFC-equivalent params hash identically. Construction: "
                "frisket.execution.action_identity.action_identity_hash."
            ),
        ),
        HashFamily(
            DOMAIN_STANDING_POLICY,
            note="standing consent policy parameters (threshold, currency)",
        ),
        HashFamily(
            DOMAIN_TEST,
            note=(
                "TEST-ONLY: the domain tests use to exercise the shared "
                "hasher's rules (float rejection, key-order invariance, "
                "domain separation) without borrowing a production family's "
                "identity. Registered because ``content_hash`` refuses "
                "unregistered domains — no caller mints a family by "
                "typo. Nothing in src/ may hash under it."
            ),
        ),
    )
}


def _canonicalize(obj: Any) -> Any:
    """NFC-normalize strings; reject floats and non-JSON types."""
    if obj is None or isinstance(obj, bool) or isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        raise ValueError("no floats in hashed material (quantities are strings/ints)")
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, (list, tuple)):
        return [_canonicalize(v) for v in obj]
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ValueError(f"hashed material keys must be strings: {k!r}")
            out[unicodedata.normalize("NFC", k)] = _canonicalize(v)
        return out
    raise ValueError(f"unhashable material of type {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    """Canonical JSON per the hash contract: sorted keys, compact
    separators, NFC strings, no floats. Raises ``ValueError`` on material
    that violates the contract (this is the authoring/hashing path, not the
    total evaluator — the evaluator never hashes)."""
    return json.dumps(
        _canonicalize(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def content_hash(domain: str, obj: Any) -> str:
    """THE content-hash implementation: bare-hex sha256 over
    ``<domain>:<canonical json>`` under the domain's declared family rules
    (:data:`HASH_FAMILIES`). Domain prefixes give unrelated hash families
    disjoint codomains (no cross-domain collisions by construction).

    An UNREGISTERED domain raises (E-2). It used to mint a strict-defaults
    family on the spot, which meant a typo'd domain string produced a
    perfectly valid-looking digest under a family nobody declared, nothing
    pinned in the corpus, and no reader — recorded identity from a name that
    does not exist. Registration is one line; minting was silent.
    """
    family = HASH_FAMILIES.get(domain)
    if family is None:
        raise ValueError(
            f"unregistered hash-family domain {domain!r} — declare it in "
            "HASH_FAMILIES (and pin it in the corpus) before hashing under it"
        )
    # Every declared family currently uses the strict rules; the registry
    # asserts that stays intentional (a divergent family is a NEW version
    # with its own branch here, never a silent drift).
    if (family.nfc_strings, family.float_policy, family.output) != (
        True,
        "reject",
        "hex",
    ):  # pragma: no cover - no such family exists
        raise ValueError(f"unimplemented hash-family rules for {domain!r}")
    body = f"{domain}:{canonical_json(obj)}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def decimal_string(value: Any) -> str:
    """Canonical plain decimal string (no exponent, no trailing zeros) for
    known-numeric hash inputs — the ONE sanctioned way a float/int quantity
    enters hashed material (``1`` and ``1.0`` become the same ``"1"``)."""
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError(f"non-finite quantity: {value!r}")
    text = format(parsed, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _as_decimal(value: Any) -> Decimal | None:
    """Quantity parse: int, decimal string, or (runtime-fact) float ->
    finite Decimal; anything else -> None. bool is not a quantity."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, float):
        # Floats never appear in hashed promise material, but candidate
        # bindings are runtime facts and may carry them.
        try:
            parsed = Decimal(str(value))
        except InvalidOperation:
            return None
        return parsed if parsed.is_finite() else None
    if isinstance(value, str):
        try:
            parsed = Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return None
        return parsed if parsed.is_finite() else None
    return None


# ---------------------------------------------------------------------------
# Versioned side tables (D1): append-only registry, publish-only
# ---------------------------------------------------------------------------


class RegistryError(Exception):
    pass


_REF_PATTERN = re.compile(r"^[a-z0-9_]+\.v[0-9]+$")


def is_table_ref(ref: Any) -> bool:
    """Whether ``ref`` is a well-formed versioned table reference
    (``"name.vN"`` — e.g. ``"egress.v1"``)."""
    return isinstance(ref, str) and _REF_PATTERN.match(ref) is not None


@dataclass(frozen=True)
class OrderTable:
    """A partial order: one safety chain (rank = index, safest first) plus
    unordered members that compare only reflexively.

    ``le(a, b)``: is ``a`` as-safe-or-safer than ``b``? True/False when
    determinate; ``None`` when either element is unknown to this table
    (unknown -> unevaluable upstream; incomparable-but-known -> False, a
    determinate non-satisfaction).
    """

    chain: tuple[str, ...]
    unordered: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        members = self.chain + self.unordered
        if len(set(members)) != len(members):
            raise ValueError("order table members must be unique")

    def le(self, a: Any, b: Any) -> bool | None:
        if a not in self.chain and a not in self.unordered:
            return None
        if b not in self.chain and b not in self.unordered:
            return None
        if a == b:
            return True
        if a in self.unordered or b in self.unordered:
            return False
        return self.chain.index(a) <= self.chain.index(b)


@dataclass(frozen=True)
class ThroughputTable:
    """Relative throughput per hardware class (decimal strings — no floats
    in recorded/pinned material). Re-projection:
    ``projected_qty = estimated_qty * factor(basis_hw) / factor(cand_hw)``
    — a slower candidate class needs more unit-time for the same work."""

    factors: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        names = [name for name, _ in self.factors]
        if len(set(names)) != len(names):
            raise ValueError("throughput table classes must be unique")
        for name, raw in self.factors:
            value = _as_decimal(raw)
            if value is None or value <= 0:
                raise ValueError(
                    f"throughput factor for {name!r} must be a positive "
                    f"decimal string: {raw!r}"
                )

    def factor(self, hardware_class: Any) -> Decimal | None:
        for name, raw in self.factors:
            if name == hardware_class:
                return Decimal(raw)
        return None


class VersionedRegistry:
    """Append-only registry of (kind, ref) -> table, ref = ``"name.vN"``.

    Publishing an existing key raises; there is deliberately no update or
    delete. A semantic change to a table is a NEW version. Every version any
    recorded promise has ever referenced is pinned forever by the frozen
    corpus (``tests/execution/route_corpus/``).
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], Any] = {}

    def publish(self, kind: str, ref: str, payload: Any) -> None:
        if not _REF_PATTERN.match(ref):
            raise RegistryError(f"table ref must look like 'name.vN': {ref!r}")
        key = (kind, ref)
        if key in self._entries:
            raise RegistryError(f"append-only: {key} already published")
        self._entries[key] = payload

    def order_table(self, ref: Any) -> OrderTable | None:
        entry = self._entries.get(("order", ref)) if isinstance(ref, str) else None
        return entry if isinstance(entry, OrderTable) else None

    def throughput_table(self, ref: Any) -> ThroughputTable | None:
        entry = self._entries.get(("throughput", ref)) if isinstance(ref, str) else None
        return entry if isinstance(entry, ThroughputTable) else None


def builtin_registry() -> VersionedRegistry:
    reg = VersionedRegistry()
    # egress.v1 — the §2.2 lattice: none < operator_lan <
    # frisket_dedicated_org < frisket_shared; third_party_api is a member
    # but unordered against the frisket chain (never auto-substitutable).
    reg.publish(
        "order",
        EGRESS_ORDER_REF,
        OrderTable(
            chain=(
                "none",
                "operator_lan",
                "frisket_dedicated_org",
                "frisket_shared",
            ),
            unordered=("third_party_api",),
        ),
    )
    return reg


REGISTRY = builtin_registry()


# ---------------------------------------------------------------------------
# Promise rows and PromiseSet
# ---------------------------------------------------------------------------


def _frozen_basis(basis: Mapping[str, Any] | None) -> str | None:
    if basis is None:
        return None
    if not isinstance(basis, Mapping):
        raise ValueError("basis must be a mapping or None")
    return canonical_json(basis)


@dataclass(frozen=True)
class Promise:
    """One compiled predicate row. Immutable; basis held as canonical JSON
    text so the row is frozen by content. ``extras_json`` preserves unknown
    row keys verbatim (D3): decode never drops what a newer writer wrote."""

    field: str
    op: str
    value: Any
    basis_json: str | None = None
    order_ref: str | None = None
    audience: str = "system_promise"
    extras_json: str = "{}"

    @staticmethod
    def make(
        field: str,
        op: str,
        value: Any = None,
        *,
        basis: Mapping[str, Any] | None = None,
        order_ref: str | None = None,
        audience: str = "system_promise",
    ) -> "Promise":
        """Strict authoring constructor (the compiler's path).

        Stored rows go through ``from_row``: it enforces the current shape
        and retired vocabulary while preserving genuinely future additions.
        """
        allowed_ops = PROMISE_FIELDS.get(field)
        if allowed_ops is None:
            raise ValueError(f"unknown promise field: {field!r}")
        if op not in allowed_ops:
            raise ValueError(f"op {op!r} not allowed for field {field!r}")
        if audience not in AUDIENCES:
            raise ValueError(f"unknown audience: {audience!r}")
        if op == "satisfies_order" and order_ref is None:
            raise ValueError("satisfies_order requires an order_ref")
        if order_ref is not None and not _REF_PATTERN.match(order_ref):
            raise ValueError(f"order_ref must look like 'name.vN': {order_ref!r}")
        # Hash-contract enforcement at the source: authored rows carry no
        # floats (canonical_json raises on float basis values too).
        _canonicalize(value)
        return Promise(
            field=field,
            op=op,
            value=value,
            basis_json=_frozen_basis(basis),
            order_ref=order_ref,
            audience=audience,
        )

    @property
    def basis(self) -> dict[str, Any] | None:
        return None if self.basis_json is None else json.loads(self.basis_json)

    def to_row(self) -> dict[str, Any]:
        row = dict(json.loads(self.extras_json))
        row.update(
            field=self.field,
            op=self.op,
            value=self.value,
            basis=self.basis,
            order_ref=self.order_ref,
            audience=self.audience,
        )
        return row

    @staticmethod
    def from_row(row: Mapping[str, Any]) -> "Promise":
        """Decode the current row schema; preserve only future extra keys."""
        retired = {"severity", "mode"} & set(row)
        if retired:
            raise ValueError(f"promise row carries retired key(s): {sorted(retired)!r}")
        core = set(HASHED_PROMISE_KEYS)
        missing = core - set(row)
        if missing:
            raise ValueError(
                f"promise row is missing required key(s): {sorted(missing)!r}"
            )
        if row.get("field") == "credential_source":
            raise ValueError("promise row carries retired field: 'credential_source'")
        extras = {k: v for k, v in row.items() if k not in core}
        basis = row.get("basis")
        return Promise(
            field=str(row["field"]),
            op=str(row["op"]),
            value=row.get("value"),
            basis_json=(
                json.dumps(basis, sort_keys=True, separators=(",", ":"))
                if basis is not None
                else None
            ),
            order_ref=row.get("order_ref"),
            audience=str(row["audience"]),
            extras_json=json.dumps(extras, sort_keys=True, separators=(",", ":")),
        )


def _hashed_row(promise: Promise) -> dict[str, Any]:
    row = promise.to_row()
    return {key: row.get(key) for key in HASHED_PROMISE_KEYS}


def promise_row_fingerprint(row: Mapping[str, Any]) -> str:
    """Identity of one promise row given as a raw row mapping:
    the ``FINGERPRINT_KEYS`` projection under ``frisket.promise_row.v1`` —
    the ONE fingerprint construction; the store's ``route_violations``
    writer and every diagnostics consumer share it."""
    return content_hash(
        DOMAIN_PROMISE_ROW, {key: row.get(key) for key in FINGERPRINT_KEYS}
    )


def promise_rows_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    """THE promise-set content hash over raw row mappings: the list of
    ``HASHED_PROMISE_KEYS`` projections, in row order, under
    ``frisket.promise_set.v1`` — the store/wire document shape (F3: one
    shape, one domain). Decoded extras and display-only fields never shift
    the hash."""
    validated = [Promise.from_row(row) for row in rows]
    return content_hash(DOMAIN_PROMISE_SET, [_hashed_row(row) for row in validated])


def promise_set_hash(promises: Sequence[Promise]) -> str:
    """Content hash of a promise set: the consent-binding identity (§5.2
    echoes it through the 402 confirm). Delegates to
    :func:`promise_rows_hash` — the single construction shared with the
    store column and the 402/confirm echo."""
    return promise_rows_hash([_hashed_row(p) for p in promises])


def basis_identity(promise: Promise) -> str:
    """The comparability class of a quantitative row.

    Every term that can change settlement enters this identity. A meter,
    scale, ceiling, row rule, rounding rule, charge authority, rate, or terms
    version change therefore cannot ratchet against consent minted under the
    old semantics merely because both offerings retained the same SKU.
    """
    basis = promise.basis or {}
    settlement_identity = {
        "pricing_key": basis.get("pricing_key"),
        "unit_rate": basis.get("unit_rate"),
        "order_ref": promise.order_ref,
        "hardware_class": basis.get("hardware_class"),
        "throughput_ref": basis.get("throughput_ref"),
        **{key: basis.get(key) for key in LIVE_COST_TERM_KEYS},
    }
    if basis.get("ceiling_mode") == "consented_quantity":
        settlement_identity["estimated_quantity"] = basis.get("estimated_quantity")
    if basis.get("row_settlement_mode") == "quoted_successful_rows":
        settlement_identity["row_quote_quantities"] = basis.get("row_quote_quantities")
    return content_hash(
        DOMAIN_BASIS_IDENTITY,
        settlement_identity,
    )


@dataclass(frozen=True)
class PromiseSet:
    """Immutable compiled promise set. Persistence identity/columns
    (subject, seq, consent linkage) belong to the store layer; this type is
    the content — rows plus their content hash. Unknown top-level document
    keys survive round-trips in ``extras_json`` (D3)."""

    promises: tuple[Promise, ...]
    extras_json: str = "{}"

    @staticmethod
    def make(promises: Sequence[Promise]) -> "PromiseSet":
        return PromiseSet(promises=tuple(promises))

    @property
    def set_hash(self) -> str:
        return promise_set_hash(self.promises)

    def to_json(self) -> str:
        doc = dict(json.loads(self.extras_json))
        doc["promises"] = [p.to_row() for p in self.promises]
        return json.dumps(doc, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def from_json(blob: str) -> "PromiseSet":
        doc = json.loads(blob)
        extras = {k: v for k, v in doc.items() if k != "promises"}
        return PromiseSet(
            promises=tuple(Promise.from_row(r) for r in doc["promises"]),
            extras_json=json.dumps(extras, sort_keys=True, separators=(",", ":")),
        )


# ---------------------------------------------------------------------------
# The generic evaluator (three-valued, total: never raises on junk rows)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalResult:
    status: str  # satisfied | violated | unevaluable
    reason: str | None = None  # open family: reasons may grow, never renumber
    detail: str | None = None


def _eval_le_cost(
    promise: Promise, fact: Any, registry: VersionedRegistry
) -> EvalResult:
    """``le`` with a pricing basis (D2). ``fact`` is the candidate's cost
    facts mapping: {pricing_key, unit_rate?, hardware_class?}.

    The RATE is fact-only (E-1). It used to fall back to
    ``basis["unit_rate"]`` — the promise's own consented rate — which made
    the predicate self-referential: a candidate that reports no rate was
    scored against the rate the user consented to, so a price increase could
    never be caught. An unobservable rate is now honestly ``unevaluable``
    and retained as diagnostic truth, never tautologically satisfied.
    Admission turns that non-satisfied authorized predicate into the typed
    re-confirmation halt.

    The QUANTITY still comes from the basis: quantity is the admission
    gate's job, not the binding's. The estimate is a property of the
    invocation (how many audio seconds this run selected), fixed when the
    user consented; the run-start binding knows the venue and its rate, and
    has no independent count to offer. Re-deriving one here would be a
    second estimate pass — the thing §0 exists to prevent — so the consented
    quantity is the honest multiplicand and a scope change is caught by the
    companion ``work_scope`` basis predicate, not by this arithmetic.
    """
    basis = promise.basis or {}
    bound = _as_decimal(promise.value)
    if bound is None:
        return EvalResult(UNEVALUABLE, "type_error", "bound value is not numeric")
    if not isinstance(fact, Mapping):
        return EvalResult(UNEVALUABLE, "type_error", "cost fact is not a mapping")
    if fact.get("pricing_key") != basis.get("pricing_key"):
        return EvalResult(  # D2: a different currency is not evidence of excess
            UNEVALUABLE,
            "basis_mismatch",
            f"promised under {basis.get('pricing_key')!r}, "
            f"candidate priced under {fact.get('pricing_key')!r}",
        )
    for key in LIVE_COST_TERM_KEYS:
        if key not in fact or fact.get(key) != basis.get(key):
            return EvalResult(
                UNEVALUABLE,
                "basis_mismatch",
                f"promised {key}={basis.get(key)!r}, "
                f"candidate reported {fact.get(key)!r}",
            )
    rate = _as_decimal(fact.get("unit_rate"))  # fact-only: never the promise's own
    qty = _as_decimal(basis.get("estimated_quantity"))  # the consented estimate
    if rate is None or qty is None:
        return EvalResult(UNEVALUABLE, "missing_fact", "no numeric rate/quantity")

    scale = Decimal(1)
    basis_hw = basis.get("hardware_class")
    candidate_hw = fact.get("hardware_class")
    throughput_ref = basis.get("throughput_ref")
    if basis_hw is None:
        # throughput_ref null + no hardware_class = hardware-invariant
        # pricing (explicit sentinel): candidate hardware is irrelevant.
        pass
    elif candidate_hw is None:
        return EvalResult(UNEVALUABLE, "missing_fact", "candidate hardware_class")
    elif candidate_hw != basis_hw:
        if throughput_ref is None:
            return EvalResult(
                UNEVALUABLE,
                "missing_throughput_ref",
                f"{basis_hw!r}->{candidate_hw!r} with no pinned table",
            )
        table = registry.throughput_table(throughput_ref)
        if table is None:
            return EvalResult(
                UNEVALUABLE, "unknown_throughput_table", repr(throughput_ref)
            )
        basis_factor = table.factor(basis_hw)
        candidate_factor = table.factor(candidate_hw)
        if basis_factor is None or candidate_factor is None:
            return EvalResult(
                UNEVALUABLE,
                "unknown_hardware_class",
                f"{basis_hw!r}->{candidate_hw!r}",
            )
        scale = basis_factor / candidate_factor  # slower candidate -> more time

    projected = rate * qty * scale
    if projected <= bound:
        return EvalResult(SATISFIED, detail=f"projected {projected} <= {bound}")
    return EvalResult(VIOLATED, "bound_exceeded", f"projected {projected} > {bound}")


def evaluate(
    promise: Promise,
    binding: Mapping[str, Any],
    registry: VersionedRegistry = REGISTRY,
) -> EvalResult:
    """Three-valued, TOTAL: any row this reader cannot understand ->
    unevaluable with a reason from the open family; it never raises."""
    try:
        if promise.op not in KNOWN_OPS:
            return EvalResult(UNEVALUABLE, "unknown_op", repr(promise.op))  # D3
        basis = promise.basis or {}
        expected_scope = basis.get("work_scope")
        if expected_scope is not None:
            if not isinstance(expected_scope, Mapping):
                return EvalResult(
                    UNEVALUABLE,
                    "invalid_work_scope",
                    "promise work_scope basis is not a mapping",
                )
            observed_scope = binding.get("work_scope")
            if observed_scope is None:
                return EvalResult(
                    UNEVALUABLE,
                    "missing_fact",
                    "work_scope",
                )
            if observed_scope != expected_scope:
                return EvalResult(
                    VIOLATED,
                    "work_scope_changed",
                    "the selected rows or their live source revisions changed",
                )
        if promise.op == "unbounded":
            # Trivially satisfied: the row promises nothing — its PRESENCE
            # (a user_claim requiring consent) is the claim.
            return EvalResult(SATISFIED, detail="unbounded promises nothing")
        if promise.field not in binding:
            return EvalResult(UNEVALUABLE, "missing_fact", promise.field)
        fact = binding[promise.field]

        if promise.op == "eq":
            if fact == promise.value:
                return EvalResult(SATISFIED)
            return EvalResult(VIOLATED, "not_equal", f"{fact!r} != {promise.value!r}")

        if promise.op == "satisfies_order":
            if promise.order_ref is None:
                return EvalResult(UNEVALUABLE, "missing_order_ref", promise.field)
            table = registry.order_table(promise.order_ref)
            if table is None:
                return EvalResult(
                    UNEVALUABLE, "unknown_order_table", repr(promise.order_ref)
                )
            comparison = table.le(fact, promise.value)
            if comparison is None:
                return EvalResult(
                    UNEVALUABLE,
                    "unknown_element",
                    f"{fact!r} or {promise.value!r}",
                )
            if comparison:
                return EvalResult(
                    SATISFIED,
                    detail=f"{fact!r} <= {promise.value!r} in {promise.order_ref}",
                )
            return EvalResult(
                VIOLATED, "order_exceeded", f"{fact!r} !<= {promise.value!r}"
            )

        # op == "le"
        if "pricing_key" in basis:
            return _eval_le_cost(promise, fact, registry)
        bound = _as_decimal(promise.value)
        fact_value = _as_decimal(fact)
        if bound is None or fact_value is None:
            return EvalResult(
                UNEVALUABLE, "type_error", f"le over {type(fact).__name__}"
            )
        if fact_value <= bound:
            return EvalResult(SATISFIED)
        return EvalResult(VIOLATED, "bound_exceeded", f"{fact_value} > {bound}")
    except Exception as exc:  # totality: junk never escapes as an exception
        return EvalResult(UNEVALUABLE, "evaluator_error", repr(exc))


# ---------------------------------------------------------------------------
# Set evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SetEvaluation:
    results: tuple[tuple[Promise, EvalResult], ...]
    overall: str


def evaluate_set(
    promises: Sequence[Promise],
    binding: Mapping[str, Any],
    registry: VersionedRegistry = REGISTRY,
) -> SetEvaluation:
    rows: list[tuple[Promise, EvalResult]] = []
    for promise in promises:
        result = evaluate(promise, binding, registry)
        rows.append((promise, result))
    statuses = [result.status for _, result in rows]
    if all(status == SATISFIED for status in statuses):
        overall = SATISFIED
    elif any(status == VIOLATED for status in statuses):
        overall = VIOLATED
    else:
        overall = UNEVALUABLE
    return SetEvaluation(tuple(rows), overall)


# ---------------------------------------------------------------------------
# Coverage envelope, keyed by (field, op, basis_identity)
#
# (``effective_promises``/``ratchet_key`` — a most-permissive-consented VIEW
# over the same chain — lived here with zero src callers; the envelope
# superseded them and they were deleted. So was ``requires_gate``: the
# gate predicate is one line, and validation.py spells it out at the one place
# that decides.)
# ---------------------------------------------------------------------------


def _envelope_key(field: str, op: str, identity: str) -> str:
    return canonical_json([field, op, identity])


def compute_coverage_envelope(
    history: Sequence[PromiseSet],
) -> dict[str, dict[str, Any]]:
    """Fold a chain of CONSENTED sets into the high-water coverage envelope.

    Only monotone quantitative rows contribute: ``le`` entries keep the
    maximum consented bound per (field, op, basis_identity); ``unbounded``
    entries record presence. Categorical and interacting claims are
    deliberately ABSENT — their only coverage is the exact promise-set-hash
    match or verbatim row recurrence (§5.3; component-wise historical
    coverage could synthesize combinations never jointly consented). Rows
    whose bound this reader cannot compare (unknown ops, non-numeric bounds —
    unevaluable ancestors) and duplicate rows never extend the mark.

    Computed on demand from the chain, never cached. (A per-append
    ``promise_sets.coverage_envelope_json`` cache and an incremental
    ``extend_coverage_envelope`` fold made this read O(claims) instead of
    O(chain x claims), but the only reachable
    caller walks the same chain two lines later regardless, so the cache
    saved nothing and both were deleted.)
    """
    out: dict[str, dict[str, Any]] = {}
    for promise_set in history:
        for promise in promise_set.promises:
            if promise.op not in ("le", "unbounded"):
                continue
            identity = basis_identity(promise)
            key = _envelope_key(promise.field, promise.op, identity)
            entry = {
                "field": promise.field,
                "op": promise.op,
                "basis_identity": identity,
                "value": promise.value,
            }
            if promise.op == "unbounded":
                out[key] = entry
                continue
            bound = _as_decimal(promise.value)
            if bound is None:
                continue  # uncomparable bound never becomes the high-water mark
            previous = out.get(key)
            previous_bound = (
                _as_decimal(previous.get("value")) if previous is not None else None
            )
            if previous_bound is None or bound > previous_bound:
                out[key] = entry
    return out


def claim_covered_by_envelope(envelope: Mapping[str, Any], promise: Promise) -> bool:
    """Component-wise coverage, monotone quantitative rows ONLY (§5.3): an
    ``le`` claim is covered by a consented bound >= the candidate bound
    under the same basis_identity; ``unbounded`` only by an explicitly
    consented ``unbounded`` on the same field + basis. Everything else
    (categorical, interacting, unknown ops) returns False — its only
    coverage is the exact promise-set-hash match, which is the store
    layer's check, not this helper's."""
    if promise.op == "unbounded":
        key = _envelope_key(promise.field, "unbounded", basis_identity(promise))
        return key in envelope
    if promise.op == "le":
        key = _envelope_key(promise.field, "le", basis_identity(promise))
        entry = envelope.get(key)
        if entry is None:
            return False
        consented = _as_decimal(entry.get("value"))
        candidate = _as_decimal(promise.value)
        if consented is None or candidate is None:
            return False
        return consented >= candidate
    return False
