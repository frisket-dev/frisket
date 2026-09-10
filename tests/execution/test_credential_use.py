"""The pre-effect credential-USE constraint.

What the credential-use fence replaced: a compiled ``credential_source`` promise evaluated at run
start, which compared the route row to itself where it could be evaluated and
was absent where it mattered. What it is now: the consent names a credential
CLASS (derived from the funding the confirm dialog already renders as "who
pays"), and the adapter's SELECTED credential must satisfy it before any
external call.
"""

from __future__ import annotations

import pytest

from frisket.execution.credential_use import (
    ENV_KEY,
    NO_CREDENTIAL,
    ORG_BYOK_KEY,
    PLATFORM_KEY,
    PROJECT_KEY,
    CredentialOwner,
    CredentialUseContext,
    CredentialUseRefusal,
    consented_credential_classes,
    credential_class_of,
    credential_classes_for_funding,
    pinned_credential_source,
    require_consented_credential,
)
from frisket.execution.price_book import (
    ByokZero,
    OperatorBorne,
    PlatformMetered,
    cost_posture_for,
)


def _honest_context(cost_posture: str) -> CredentialUseContext:
    if cost_posture == "platform_metered":
        owner = CredentialOwner.deployment("test-hosted-deployment")
        return CredentialUseContext(
            cost_posture=cost_posture,
            consented_owner=owner,
            selected_owner=owner,
            deployment_owner=owner,
        )
    if cost_posture == "org_key":
        owner = CredentialOwner.organization("test-organization")
        return CredentialUseContext(
            cost_posture=cost_posture,
            consented_owner=owner,
            selected_owner=owner,
            deployment_owner=CredentialOwner.deployment("test-hosted-deployment"),
        )
    return CredentialUseContext.open()


# ---------------------------------------------------------------------------
# The two maps, and the fact that they agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "funding,expected",
    [
        # "billed to your own account" names no particular key of the user's,
        # so any of the user's own satisfies it.
        (OperatorBorne(), frozenset({ENV_KEY, PROJECT_KEY, ORG_BYOK_KEY})),
        (PlatformMetered(), frozenset({PLATFORM_KEY})),
        # "billed to your organization's provider key" names exactly one.
        (ByokZero(), frozenset({ORG_BYOK_KEY})),
    ],
)
def test_each_funding_class_names_its_credential_classes(funding, expected):
    assert credential_classes_for_funding(funding) == expected


@pytest.mark.parametrize("funding", [OperatorBorne(), PlatformMetered(), ByokZero()])
def test_the_pinned_source_belongs_to_the_classes_its_funding_names(funding):
    """One authority, two spellings: the token the route row PINS and the
    classes the adapter is checked against are derived from the same funding
    fact, so a route written by the resolver can never fail its own fence."""
    assert credential_class_of(pinned_credential_source(funding)) in (
        credential_classes_for_funding(funding)
    )
    assert consented_credential_classes(cost_posture_for(funding)) == (
        credential_classes_for_funding(funding)
    )


@pytest.mark.parametrize(
    "source,expected",
    [
        ("platform_key", PLATFORM_KEY),
        ("local", ENV_KEY),
        ("project_key", PROJECT_KEY),
        ("org_byok", ORG_BYOK_KEY),
        ("none", NO_CREDENTIAL),
        ("cache", NO_CREDENTIAL),
        ("a-token-from-2029", NO_CREDENTIAL),  # fail closed on the unknown
        (None, NO_CREDENTIAL),
    ],
)
def test_every_source_token_classifies_and_the_unknown_fails_closed(source, expected):
    assert credential_class_of(source) == expected


def test_the_whole_source_vocabulary_is_classified_explicitly():
    """Closure, not a promise that someone enumerated correctly: every token
    the model layer can stamp has a DECIDED class here. An unclassified one
    would fall to ``no_credential`` and refuse every run under it — fail
    closed, but silently and for the wrong reason."""
    from frisket.ai.llm.types import CREDENTIAL_SOURCES
    from frisket.execution.credential_use import _SOURCE_CLASS

    assert set(_SOURCE_CLASS) == set(CREDENTIAL_SOURCES)


# ---------------------------------------------------------------------------
# The constraint itself
# ---------------------------------------------------------------------------


def test_the_users_own_key_satisfies_an_operator_borne_consent():
    """The product sentence: a run consented as "billed to your own account"
    accepts ANY of the operator's own credentials — the env key, the project
    key, the org's BYOK key — because that dialog names no particular one.
    The fence RETURNS which one it was, so the ledger records the credential
    that actually ran the work rather than a collapsed family name."""
    for source, expected in (
        ("local", ENV_KEY),
        ("project_key", PROJECT_KEY),
        ("org_byok", ORG_BYOK_KEY),
    ):
        assert (
            require_consented_credential(
                cost_posture="operator_borne",
                selected_source=source,
                context=_honest_context("operator_borne"),
                effect="the call",
            )
            == expected
        )


def test_an_env_key_never_satisfies_an_org_byok_consent():
    """THE critical this split exists for. An org confirms "billed to your
    organization's provider key"; the deployment's process environment also
    holds a key for the same provider; the selector honestly reports ``local``
    because that env key is what resolved. Under one collapsed
    ``operator_key`` class the fence compared operator_key to operator_key and
    passed — every page went out and was billed on the operator's shared key,
    and the one claim the user made about credentials was never checked. It
    must refuse BEFORE the call."""
    with pytest.raises(CredentialUseRefusal) as excinfo:
        require_consented_credential(
            cost_posture="org_key",
            selected_source="local",
            context=_honest_context("org_key"),
            effect="the hosted Datalab OCR call",
        )
    message = str(excinfo.value)
    assert "the hosted Datalab OCR call" in message
    assert "this deployment's environment credential" in message
    assert "your organization's own provider key" in message
    assert "refusing before the call" in message


def test_a_project_key_never_satisfies_an_org_byok_consent():
    """The same rule for the other non-BYOK credential of the user's: a key
    in this project's secrets store is not the organization's key, and a
    consent that named the organization's key is not evidence for it."""
    with pytest.raises(CredentialUseRefusal):
        require_consented_credential(
            cost_posture="org_key",
            selected_source="project_key",
            context=_honest_context("org_key"),
            effect="the call",
        )


def test_the_org_byok_key_satisfies_its_own_consent():
    """The fence is not merely restrictive: the credential the BYOK consent
    actually named passes, so a correctly-configured org run dispatches."""
    assert (
        require_consented_credential(
            cost_posture="org_key",
            selected_source="org_byok",
            context=_honest_context("org_key"),
            effect="the call",
        )
        == ORG_BYOK_KEY
    )


def test_a_platform_key_under_a_byok_consent_refuses_before_the_call():
    """O3's hazard: a silent fallback to the platform key on a run the user
    consented to pay for with their organization's key. It never becomes a
    charge, because it never becomes a call."""
    with pytest.raises(CredentialUseRefusal) as excinfo:
        require_consented_credential(
            cost_posture="org_key",
            selected_source="platform_key",
            context=_honest_context("org_key"),
            effect="this openai transcription call",
        )
    message = str(excinfo.value)
    assert "this openai transcription call" in message
    assert "the platform credential" in message
    assert "your organization's own provider key" in message
    assert "refusing before the call" in message


def test_the_users_own_key_under_a_platform_metered_consent_refuses():
    """The mirror case, and the reason the constraint is symmetric: a metered
    run the user was quoted a platform price for must not quietly bill
    their own provider account instead."""
    with pytest.raises(CredentialUseRefusal):
        require_consented_credential(
            cost_posture="platform_metered",
            selected_source="local",
            context=_honest_context("platform_metered"),
            effect="the hosted call",
        )


def test_no_credential_at_all_never_satisfies_a_consent():
    """An unconfigured provider selects nothing. "Nothing" is not the user's
    key, so the fence says so by name rather than letting the adapter fail
    later with an opaque auth error."""
    for posture in ("operator_borne", "platform_metered", "org_key"):
        with pytest.raises(CredentialUseRefusal):
            require_consented_credential(
                cost_posture=posture,
                selected_source="none",
                context=_honest_context(posture),
                effect="the call",
            )


def test_an_unknown_cost_posture_refuses_rather_than_widening_the_consent():
    """The posture vocabulary is closed and the price book is its only
    writer, so a posture this reader does not recognize is a corrupt route
    row. Guessing operator-borne would WIDEN the accepted classes to
    env/project/org keys — a fence failing open on unreadable data — so the
    derivation refuses and names the value instead."""
    with pytest.raises(ValueError, match="unknown cost posture"):
        consented_credential_classes("included")


# ---------------------------------------------------------------------------
# W4 owner half: the same class is not sufficient evidence of the payer
# ---------------------------------------------------------------------------


def test_platform_credential_requires_the_consented_deployment_owner():
    owner = CredentialOwner.deployment("hosted-prod")
    context = CredentialUseContext(
        cost_posture="platform_metered",
        consented_owner=owner,
        selected_owner=owner,
        deployment_owner=owner,
    )

    assert (
        require_consented_credential(
            cost_posture="platform_metered",
            selected_source="platform_key",
            selected_owner=owner,
            context=context,
            effect="the call",
        )
        == PLATFORM_KEY
    )


def test_same_class_but_different_byok_owner_refuses_before_effect():
    funding_owner = CredentialOwner.organization(31)
    storage_owner = CredentialOwner.organization(44)
    context = CredentialUseContext(
        cost_posture="org_key",
        consented_owner=funding_owner,
        selected_owner=storage_owner,
        deployment_owner=CredentialOwner.deployment("hosted-prod"),
    )

    with pytest.raises(CredentialUseRefusal) as excinfo:
        require_consented_credential(
            cost_posture="org_key",
            selected_source="org_byok",
            selected_owner=storage_owner,
            context=context,
            effect="the S!=F provider call",
        )
    message = str(excinfo.value)
    assert "organization '44'" in message
    assert "organization '31'" in message
    assert "refusing before the call" in message


def test_hosted_class_without_both_owner_facts_refuses():
    with pytest.raises(CredentialUseRefusal, match="both sides"):
        require_consented_credential(
            cost_posture="platform_metered",
            selected_source="platform_key",
            context=CredentialUseContext(cost_posture="platform_metered"),
            effect="the call",
        )


def test_request_posture_must_equal_the_quote_posture():
    owner = CredentialOwner.deployment("hosted-prod")
    with pytest.raises(CredentialUseRefusal, match="context and quote disagree"):
        require_consented_credential(
            cost_posture="platform_metered",
            selected_source="platform_key",
            selected_owner=owner,
            context=CredentialUseContext(
                cost_posture="operator_borne",
                selected_owner=owner,
                deployment_owner=owner,
            ),
            effect="the call",
        )


def test_owner_kind_must_match_the_funding_posture():
    malformed_owner = CredentialOwner.organization("same-spelling")
    with pytest.raises(CredentialUseRefusal, match="must identify a deployment"):
        require_consented_credential(
            cost_posture="platform_metered",
            selected_source="platform_key",
            selected_owner=malformed_owner,
            context=CredentialUseContext(
                cost_posture="platform_metered",
                consented_owner=malformed_owner,
                selected_owner=malformed_owner,
            ),
            effect="the call",
        )
