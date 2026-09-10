"""The pre-effect credential-use constraint.

*"Runs under MY key" is a checkable promise, not a post-hoc observation.*

An earlier compiled ``credential_source`` promise evaluated at run start was
tautological where it could be evaluated (on a zero-tariff transport both
sides of the comparison read the
same route row) and unevaluable where it mattered (on a tariffed transport the
actual provenance does not exist until the model call). It is replaced here,
not deleted: deleting it outright would leave nothing before the effect
asserting whose credential pays, and a divergence recorded after data and
spend crossed the boundary cannot answer "did I say yes to that".

The three positions, kept apart on purpose:

- **Consented** (pre-effect, an admission fact): the credential CLASSES a
  funding class may run under. It is DERIVED from the route row's
  ``cost_posture`` — the same fact ``claim_labels.cost_posture_label`` already
  renders in the confirm dialog ("billed to your own account" / "metered by
  the platform charge authority" / "billed to your organization's provider
  key") — so there is no second authority for who pays and no new column.
- **Enforced** (pre-effect, at the adapter): :func:`require_consented_credential`
  compares the credential the adapter actually SELECTED against those classes
  and refuses before the call.
- **Observed** (post-effect, evidence): the reported source stays a divergence
  check on the binding epoch (``runtime_binding``), recording divergence
  rather than normalizing it away.

**Why the classes are not two.** Collapsing ``local``, ``project_key`` and
``org_byok`` into one ``operator_key`` class would make a BYOK consent
uncheckable, which is the one consent it most needs to be. An org confirms
"billed to your organization's provider key" (``cost_posture='org_key'``); the
deployment env also holds a ``DATALAB_API_KEY``; the selector reports ``local``
because a process-env key is what resolved; the fence compares
``operator_key`` to ``operator_key`` and passes — every page is sent and billed
on the OPERATOR'S shared key, and the user's actual claim ("my org's key pays,
and my documents go out under my org's account") was never checked at all. So
each of the user's own credentials is now its own class, the BYOK consent
accepts exactly the BYOK class, and the "any of my own keys" latitude survives
where it was genuinely offered: the operator-borne posture, whose dialog says
"billed to your own account" without naming which one.

This module is the ONE home for both maps: which credential classes a funding
class may run under, and which class a concrete source token belongs to. The
resolver's route-row pin reads the first; the adapter fence reads both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from frisket.execution.price_book import (
    ByokZero,
    Funding,
    OperatorBorne,
    PlatformMetered,
    funding_for_cost_posture,
)

if TYPE_CHECKING:
    from frisket.credentials import ResolvedCredential

#: The charge authority's credential: the platform pays the provider and
#: meters the user for it.
PLATFORM_KEY = "platform_key"
#: A credential held by the DEPLOYMENT's process environment. It is the
#: operator's own key, and on a shared/hosted deployment "the operator" is not
#: the user: an env key is exactly what a BYOK consent is promising NOT to
#: use, which is why it is its own class.
ENV_KEY = "env_key"
#: A credential stored in this project's own secrets store.
PROJECT_KEY = "project_key"
#: The organization's own provider key (BYOK). The class a
#: ``cost_posture='org_key'`` consent names, and the ONLY one it accepts.
ORG_BYOK_KEY = "org_byok_key"
#: No external credential was selected at all (in-process work, a cache hit,
#: or an unconfigured provider). Never satisfies a constraint that names a
#: key — fail closed and say which.
NO_CREDENTIAL = "no_credential"

CREDENTIAL_CLASSES = (
    PLATFORM_KEY,
    ENV_KEY,
    PROJECT_KEY,
    ORG_BYOK_KEY,
    NO_CREDENTIAL,
)

#: Which credential classes each funding class may run under (§3.3/§3.5).
#: A SET per funding class, because the latitude differs by what the dialog
#: actually promised: "billed to your own account" names no particular key of
#: the user's, while "billed to your organization's provider key" names one.
_FUNDING_CREDENTIAL_CLASSES: dict[type, frozenset[str]] = {
    # The open edition: the operator runs and pays for everything, and every
    # credential in reach is theirs. Any of their own keys satisfies it; the
    # platform's does not.
    OperatorBorne: frozenset({ENV_KEY, PROJECT_KEY, ORG_BYOK_KEY}),
    # Platform-metered: the charge authority's key, or the user is being
    # charged for a call their own account paid for.
    PlatformMetered: frozenset({PLATFORM_KEY}),
    # BYOK: the organization's key, and nothing else. Not the deployment's env
    # key (that is the operator's, billed to the operator, and shared across
    # every tenant of the deployment), and not a project key.
    ByokZero: frozenset({ORG_BYOK_KEY}),
}

#: The route row's PINNED ``credential_source`` token per funding class — the
#: promise-side spelling the receipt and the post-effect divergence check
#: compare against. Lives beside the class map so the two can never drift.
FUNDING_CREDENTIAL_SOURCE: dict[type, str] = {
    OperatorBorne: "local",
    PlatformMetered: "platform_key",
    ByokZero: "org_byok",
}

#: The class each concrete ``credential_source`` token belongs to
#: (``frisket.ai.llm.types.CREDENTIAL_SOURCES`` is the token vocabulary).
#: ``local`` is the deployment's process environment — the token predates the
#: distinction and keeps its wire spelling; the CLASS is what the fence reads.
_SOURCE_CLASS: dict[str, str] = {
    "platform_key": PLATFORM_KEY,
    "local": ENV_KEY,
    "project_key": PROJECT_KEY,
    "org_byok": ORG_BYOK_KEY,
    "none": NO_CREDENTIAL,
    "cache": NO_CREDENTIAL,
}

_CLASS_COPY = {
    PLATFORM_KEY: "the platform credential",
    ENV_KEY: "this deployment's environment credential",
    PROJECT_KEY: "this project's stored credential",
    ORG_BYOK_KEY: "your organization's own provider key",
    NO_CREDENTIAL: "no credential at all",
}

_CLASSES_COPY = {
    frozenset({ENV_KEY, PROJECT_KEY, ORG_BYOK_KEY}): "your own credential",
    frozenset({PLATFORM_KEY}): _CLASS_COPY[PLATFORM_KEY],
    frozenset({ORG_BYOK_KEY}): _CLASS_COPY[ORG_BYOK_KEY],
}


@dataclass(frozen=True)
class CredentialOwner:
    """The non-secret principal whose provider account a credential charges.

    ``kind`` prevents an organization id and a deployment id that happen to
    have the same string spelling from comparing equal.  The owner travels
    beside credential provenance; it is never inferred from a project path or
    a mutable payload.
    """

    kind: Literal["deployment", "organization"]
    owner_id: str

    def __post_init__(self) -> None:
        if self.kind not in {"deployment", "organization"}:
            raise ValueError(f"unknown credential owner kind {self.kind!r}")
        if not isinstance(self.owner_id, str) or not self.owner_id.strip():
            raise ValueError("credential owner_id must be a non-empty string")

    @classmethod
    def deployment(cls, owner_id: str | int) -> CredentialOwner:
        return cls(kind="deployment", owner_id=str(owner_id))

    @classmethod
    def organization(cls, owner_id: str | int) -> CredentialOwner:
        return cls(kind="organization", owner_id=str(owner_id))


class ActionCredentialResolver(Protocol):
    """Request-scoped port for capability-specific provider credentials.

    The open implementation keeps using environment/project resolution.
    Hosted composition can inject a funding-account resolver returning a
    ``ResolvedCredential`` without putting plaintext or an owner id in an
    action payload.
    """

    def resolve_action_credential(
        self, project: Any, name: str
    ) -> ResolvedCredential | None: ...


@dataclass(frozen=True)
class CredentialUseContext:
    """Typed request facts needed by the pre-effect payer fence.

    ``consented_owner`` is the quote-bound owner. ``selected_owner`` is the
    live credential-selection owner (the trusted queue funding column on the
    queued path).  Keeping both is what lets the fence observe F1 != F2 rather
    than replacing one with the other before comparison.

    ``deployment_owner`` identifies process-held hosted credentials.  It is
    what allows the generic resolver to normalize an environment credential
    from the open-edition ``local`` spelling to ``platform_key`` *before* the
    fence sees it.
    """

    cost_posture: str = "operator_borne"
    consented_owner: CredentialOwner | None = None
    selected_owner: CredentialOwner | None = None
    deployment_owner: CredentialOwner | None = None
    credential_resolver: ActionCredentialResolver | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        # This validates the closed posture vocabulary without duplicating it.
        funding_for_cost_posture(self.cost_posture)
        if (
            self.deployment_owner is not None
            and self.deployment_owner.kind != "deployment"
        ):
            raise ValueError("deployment_owner must name a deployment principal")

    @classmethod
    def open(cls) -> CredentialUseContext:
        return cls()


def _classes_copy(classes: frozenset[str]) -> str:
    known = _CLASSES_COPY.get(classes)
    if known is not None:
        return known
    return " or ".join(sorted(_CLASS_COPY[name] for name in classes))


class CredentialUseRefusal(RuntimeError):
    """The adapter selected a credential of a class the consent did not name.

    Raised BEFORE the external call, so nothing leaves the machine and nothing
    is spent. ``code`` is the receipt error code; it is deliberately NOT
    ``consent_missing`` — a consent exists and is valid, the dispatch just
    tried to run it under the wrong key, and no reconfirmation of the same
    claims repairs that.
    """

    code = "credential_use_refused"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.remedy = message


def credential_classes_for_funding(funding: Funding) -> frozenset[str]:
    """The credential classes one funding class may run under."""
    try:
        return _FUNDING_CREDENTIAL_CLASSES[type(funding)]
    except KeyError:  # pragma: no cover - the union is closed
        raise ValueError(f"unknown funding class {funding!r}") from None


def pinned_credential_source(funding: Funding) -> str:
    """The route row's pinned ``credential_source`` token for a funding
    class — the ONE mint of that token (``resolver._edition_facts``)."""
    try:
        return FUNDING_CREDENTIAL_SOURCE[type(funding)]
    except KeyError:  # pragma: no cover - the union is closed
        raise ValueError(f"unknown funding class {funding!r}") from None


def credential_class_of(source: Any) -> str:
    """The class of a concrete selected/observed ``credential_source`` token.

    An unknown token is :data:`NO_CREDENTIAL`, never a silent match: a source
    this reader does not recognize is not evidence that the user's key was
    used.
    """
    if not isinstance(source, str):
        return NO_CREDENTIAL
    return _SOURCE_CLASS.get(source, NO_CREDENTIAL)


def consented_credential_classes(cost_posture: str) -> frozenset[str]:
    """The classes the consent named, derived from the consented route row.

    ``cost_posture`` is the route column the claims gate already renders as
    "who pays"; funding is the same fact under its other spelling. Deriving
    here keeps the constraint the user READ and the constraint the adapter is
    CHECKED against one value.
    """
    return credential_classes_for_funding(funding_for_cost_posture(cost_posture))


def _owner_for_selected_source(
    source: Any,
    *,
    selected_owner: CredentialOwner | None,
    context: CredentialUseContext,
) -> CredentialOwner | None:
    if selected_owner is not None:
        return selected_owner
    owner = context.selected_owner
    if source == "platform_key" and owner is None:
        owner = context.deployment_owner
    return owner


def _owner_copy(owner: CredentialOwner | None) -> str:
    if owner is None:
        return "no verifiable credential owner"
    noun = "deployment" if owner.kind == "deployment" else "organization"
    return f"{noun} {owner.owner_id!r}"


def require_consented_credential(
    *,
    cost_posture: str,
    selected_source: Any,
    effect: str,
    selected_owner: CredentialOwner | None = None,
    context: CredentialUseContext,
) -> str:
    """Refuse before ``effect`` unless the selected credential satisfies the
    consented classes. Returns the satisfied class (so callers can log/record
    WHICH credential actually ran the work); raises
    :class:`CredentialUseRefusal` otherwise.

    ``effect`` names what was about to happen, in the operator's words
    ("the remote transcription call"), because a refusal that cannot
    say what it stopped is not actionable.
    """
    consented = consented_credential_classes(cost_posture)
    selected = credential_class_of(selected_source)
    if selected not in consented:
        raise CredentialUseRefusal(
            f"{effect} would run under {_CLASS_COPY[selected]} "
            f"(credential source {selected_source!r}), but this run was consented "
            f"to run under {_classes_copy(consented)} "
            f"(cost posture {cost_posture!r}); refusing before the call — "
            "re-run the action to resolve (and consent to) the credential this "
            "deployment actually selects"
        )

    if context.cost_posture != cost_posture:
        raise CredentialUseRefusal(
            f"{effect} carries cost posture {context.cost_posture!r}, but the "
            f"consented route carries {cost_posture!r}; refusing before the "
            "call because the request credential context and quote disagree"
        )

    actual_owner = _owner_for_selected_source(
        selected_source,
        selected_owner=selected_owner,
        context=context,
    )
    if (
        selected_owner is not None
        and context.selected_owner is not None
        and selected_owner != context.selected_owner
    ):
        raise CredentialUseRefusal(
            f"{effect} resolved {_owner_copy(selected_owner)}, but the trusted "
            f"request credential selection names "
            f"{_owner_copy(context.selected_owner)}; refusing before the call"
        )

    expected_owner = context.consented_owner
    owner_is_required = cost_posture in {"platform_metered", "org_key"}
    if owner_is_required and (expected_owner is None or actual_owner is None):
        raise CredentialUseRefusal(
            f"{effect} selected {_owner_copy(actual_owner)}, but this "
            f"{cost_posture!r} consent does not carry both sides of the "
            "credential-owner comparison; refusing before the call"
        )
    expected_kind = {
        "platform_metered": "deployment",
        "org_key": "organization",
    }.get(cost_posture)
    if expected_kind is not None and (
        expected_owner.kind != expected_kind or actual_owner.kind != expected_kind
    ):
        raise CredentialUseRefusal(
            f"{effect} carries {_owner_copy(actual_owner)} for a "
            f"{cost_posture!r} consent naming {_owner_copy(expected_owner)}; "
            f"both credential owners must identify a {expected_kind}; "
            "refusing before the call"
        )
    if expected_owner is not None and actual_owner != expected_owner:
        raise CredentialUseRefusal(
            f"{effect} would run under {_owner_copy(actual_owner)}, but the "
            f"quote was consented for {_owner_copy(expected_owner)}; refusing "
            "before the call"
        )
    return selected
