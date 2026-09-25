from __future__ import annotations

from frisket.execution.definitions import (
    LOCAL_MODELS_TARGET_ID,
    StaticExecutionTargetProvider,
    build_static_targets,
)
from frisket.execution.targets import CAPABILITY_TO_MARKDOWN


def test_managed_local_target_is_opt_in_and_docling_only() -> None:
    assert LOCAL_MODELS_TARGET_ID not in {
        target.id for target in build_static_targets()
    }

    targets = build_static_targets(include_managed_local_models=True)
    managed = next(target for target in targets if target.id == LOCAL_MODELS_TARGET_ID)
    assert managed.operator == "self"
    assert managed.egress_class == "none"
    assert [
        (support.engine, support.capability, support.transport)
        for support in managed.engines
    ] == [("docling", CAPABILITY_TO_MARKDOWN, "sidecar.convert")]


def test_managed_local_connection_uses_private_env_and_install_fact(
    monkeypatch,
) -> None:
    monkeypatch.setattr("frisket.runtime.model_install.is_installed", lambda: True)
    provider = StaticExecutionTargetProvider(
        env={
            "FRISKET_LOCAL_MODELS_URL": "http://127.0.0.1:42117",
            "FRISKET_LOCAL_MODELS_TOKEN": "managed-secret",
            "FRISKET_MODELS_URL": "https://external.example",
            "FRISKET_MODELS_TOKEN": "external-secret",
        },
        include_managed_local_models=True,
    )

    connection = provider.connection(LOCAL_MODELS_TARGET_ID)
    assert connection is not None
    assert connection.base_url == "http://127.0.0.1:42117"
    assert connection.token == "managed-secret"
    assert connection.extra == {}

    # A provider built before installation can observe the completed setup
    # without an HTTP liveness query or process restart.
    monkeypatch.setattr("frisket.runtime.model_install.is_installed", lambda: False)
    pending = StaticExecutionTargetProvider(
        env={
            "FRISKET_LOCAL_MODELS_URL": "http://127.0.0.1:42117",
            "FRISKET_LOCAL_MODELS_TOKEN": "managed-secret",
        },
        include_managed_local_models=True,
    )
    assert pending.connection(LOCAL_MODELS_TARGET_ID) is None
    monkeypatch.setattr("frisket.runtime.model_install.is_installed", lambda: True)
    assert pending.connection(LOCAL_MODELS_TARGET_ID) is not None


def test_managed_local_connection_requires_complete_private_config(monkeypatch) -> None:
    monkeypatch.setattr("frisket.runtime.model_install.is_installed", lambda: True)
    provider = StaticExecutionTargetProvider(
        env={
            "FRISKET_MODELS_URL": "https://external.example",
            "FRISKET_MODELS_TOKEN": "external-secret",
        },
        include_managed_local_models=True,
    )

    assert provider.connection(LOCAL_MODELS_TARGET_ID) is None


def test_provider_without_managed_target_does_not_probe_gateway(monkeypatch) -> None:
    def unexpected_probe(self):
        raise AssertionError("unrelated compositions must not probe model gateways")

    monkeypatch.setattr(
        StaticExecutionTargetProvider,
        "_models_gateway_connection",
        unexpected_probe,
    )

    provider = StaticExecutionTargetProvider(include_managed_local_models=False)
    assert LOCAL_MODELS_TARGET_ID not in {target.id for target in provider.targets()}
