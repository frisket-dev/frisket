"""Behavioral coverage for ctx.secret(): manifest-declared secrets read
through the context accessor instead of raw os.environ, with the
undeclared / unprovisioned failure modes distinguished.

The accessor is shared by every surviving handler context (importer,
projection, operator), so it is exercised through them directly rather than
through a dispatch path.
"""

from __future__ import annotations

import pytest

from frisket.plugins.sdk import (
    PluginImporterContext,
    PluginOperatorContext,
    PluginProjectionContext,
    PluginUserError,
)


def _importer_context(declared: tuple[str, ...]) -> PluginImporterContext:
    return PluginImporterContext(
        project_id="p1",
        plugin_id="demo.importer",
        handler_key="demo.importer:load",
        importer_kind="demo.importer.load",
        capabilities=(),
        diagnostics=[],
        secrets=declared,
    )


def test_declared_and_provisioned_secret_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEMO_API_KEY", "s3cret-value")
    ctx = _importer_context(("DEMO_API_KEY",))
    assert ctx.secret("DEMO_API_KEY") == "s3cret-value"


def test_undeclared_secret_access_names_the_manifest_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Provisioned in the environment but NOT declared: the accessor still
    # refuses, telling the author to declare it — not a provisioning hint.
    monkeypatch.setenv("NOT_DECLARED_KEY", "present-anyway")
    ctx = _importer_context(("DEMO_API_KEY",))
    with pytest.raises(PluginUserError) as excinfo:
        ctx.secret("NOT_DECLARED_KEY")
    message = str(excinfo.value)
    assert "'NOT_DECLARED_KEY'" in message
    assert "requires.secrets" in message
    assert "not declared" in message


def test_declared_but_unprovisioned_secret_names_the_provisioning_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEMO_API_KEY", raising=False)
    ctx = _importer_context(("DEMO_API_KEY",))
    with pytest.raises(PluginUserError) as excinfo:
        ctx.secret("DEMO_API_KEY")
    message = str(excinfo.value)
    assert "'DEMO_API_KEY'" in message
    assert "no value is provisioned" in message
    assert "not declared" not in message


def test_blank_provisioned_value_counts_as_unprovisioned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEMO_API_KEY", "   ")
    ctx = _importer_context(("DEMO_API_KEY",))
    with pytest.raises(PluginUserError, match="no value is provisioned"):
        ctx.secret("DEMO_API_KEY")


@pytest.mark.parametrize(
    ("factory", "kind_field"),
    [
        (PluginProjectionContext, "projection_kind"),
        (PluginOperatorContext, "operator_kind"),
    ],
)
def test_every_handler_context_shares_the_same_accessor(
    factory, kind_field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = factory(
        project_id="p1",
        plugin_id="demo.other",
        handler_key="demo.other:run",
        capabilities=(),
        secrets=("OTHER_KEY",),
        **{kind_field: "demo.other.run"},
    )
    monkeypatch.setenv("OTHER_KEY", "other-secret")
    assert ctx.secret("OTHER_KEY") == "other-secret"
    with pytest.raises(PluginUserError, match="not declared"):
        ctx.secret("MISSING_KEY")
