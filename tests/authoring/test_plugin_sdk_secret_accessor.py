"""Behavioral coverage for ctx.secret(): manifest-declared secrets read
through the context accessor instead of raw os.environ, with the
undeclared / unprovisioned failure modes distinguished.

The accessor is shared by every surviving handler context (importer,
projection, operator), so it is exercised through them directly rather than
through a dispatch path.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from frisket.plugins.sdk import (
    PluginImporterContext,
    PluginOperatorContext,
    PluginProjectionContext,
    PluginUserError,
)


def _importer_context(
    declared: tuple[str, ...], values: dict[str, str] | None = None
) -> PluginImporterContext:
    return PluginImporterContext(
        project_id="p1",
        plugin_id="demo.importer",
        handler_key="demo.importer:load",
        importer_kind="demo.importer.load",
        capabilities=(),
        diagnostics=[],
        secrets=declared,
        _secret_values=MappingProxyType(dict(values or {})),
    )


def test_declared_and_provisioned_secret_is_returned() -> None:
    ctx = _importer_context(("DEMO_API_KEY",), {"DEMO_API_KEY": "s3cret-value"})
    assert ctx.secret("DEMO_API_KEY") == "s3cret-value"


def test_undeclared_secret_access_names_the_manifest_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Provisioned in the invocation snapshot but NOT declared: the accessor still
    # refuses, telling the author to declare it — not a provisioning hint.
    ctx = _importer_context(("DEMO_API_KEY",), {"NOT_DECLARED_KEY": "present-anyway"})
    with pytest.raises(PluginUserError) as excinfo:
        ctx.secret("NOT_DECLARED_KEY")
    message = str(excinfo.value)
    assert "'NOT_DECLARED_KEY'" in message
    assert "requires.secrets" in message
    assert "not declared" in message


def test_declared_but_unprovisioned_secret_names_the_provisioning_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    ctx = _importer_context(("DEMO_API_KEY",), {"DEMO_API_KEY": "   "})
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
        _secret_values=MappingProxyType({"OTHER_KEY": "other-secret"}),
        **{kind_field: "demo.other.run"},
    )
    assert ctx.secret("OTHER_KEY") == "other-secret"
    with pytest.raises(PluginUserError, match="not declared"):
        ctx.secret("MISSING_KEY")


def test_secret_accessor_ignores_ambient_environment_and_redacts_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "stdin-only-secret"
    monkeypatch.setenv("DEMO_API_KEY", secret)
    ctx = _importer_context(("DEMO_API_KEY",))

    with pytest.raises(PluginUserError) as excinfo:
        ctx.secret("DEMO_API_KEY")

    assert secret not in repr(ctx)
    assert secret not in str(excinfo.value)


def test_secret_snapshots_do_not_cross_calls_or_leak_through_errors() -> None:
    first = _importer_context(("DEMO_API_KEY",), {"DEMO_API_KEY": "first-value"})
    second = _importer_context(("DEMO_API_KEY",), {"DEMO_API_KEY": "second-value"})

    assert first.secret("DEMO_API_KEY") == "first-value"
    assert second.secret("DEMO_API_KEY") == "second-value"
    with pytest.raises(PluginUserError) as excinfo:
        _importer_context(("DEMO_API_KEY",), {"DEMO_API_KEY": "third-value"}).secret(
            "OTHER_KEY"
        )
    assert "third-value" not in str(excinfo.value)
