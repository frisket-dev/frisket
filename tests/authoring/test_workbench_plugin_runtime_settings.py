from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from frisket.authoring.workbench import plugin_runtime_settings as plugin_runtime
from frisket.authoring.workbench.plugin_runtime_shared import (
    WorkbenchPluginLifecycleError,
)


def _stub_loaded_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = SimpleNamespace(
        manifest=SimpleNamespace(requires=SimpleNamespace(secrets=[]))
    )
    monkeypatch.setattr(
        plugin_runtime,
        "_loaded_manifest_for_plugin_env",
        lambda project, *, plugin_id: loaded,
    )


def _assert_raw_value_absent(exc: BaseException, raw_value: str) -> None:
    assert raw_value not in str(exc)
    assert raw_value not in json.dumps(
        getattr(exc, "details", {}), sort_keys=True, default=str
    )


def test_reserved_plugin_env_name_error_never_echoes_raw_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_loaded_manifest(monkeypatch)
    raw_value = "reserved-name-raw-secret-04b"

    with pytest.raises(WorkbenchPluginLifecycleError) as raised:
        plugin_runtime.set_workbench_plugin_env_var(
            object(),
            project_id="project-04b",
            plugin_id="plugin.04b",
            name="OPENAI_API_KEY",
            value=raw_value,
        )

    assert raised.value.code == "plugin_env_name_reserved"
    _assert_raw_value_absent(raised.value, raw_value)


def test_empty_plugin_env_value_error_has_only_safe_message_and_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_loaded_manifest(monkeypatch)
    raw_value = ""

    with pytest.raises(WorkbenchPluginLifecycleError) as raised:
        plugin_runtime.set_workbench_plugin_env_var(
            object(),
            project_id="project-04b",
            plugin_id="plugin.04b",
            name="PLUGIN_04B_TOKEN",
            value=raw_value,
        )

    assert raised.value.code == "plugin_env_value_required"
    assert raised.value.message == "plugin env var value is required"
    assert raised.value.details == {}
    assert repr(raw_value) not in raised.value.message
    assert repr(raw_value) not in json.dumps(raised.value.details, sort_keys=True)


def test_plugin_env_write_reraises_without_echoing_raw_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_loaded_manifest(monkeypatch)
    raw_value = "write-failure-raw-secret-04b"
    write_error = RuntimeError("fixture plugin env write failed")

    class FailingPluginEnvDB:
        def __init__(self) -> None:
            self.rollback_calls = 0

        def execute(self, statement: str, parameters: tuple[str, ...]) -> None:
            assert "INSERT INTO workbench_plugin_env_vars" in statement
            assert raw_value not in parameters
            raise write_error

        def rollback(self) -> None:
            self.rollback_calls += 1

    database = FailingPluginEnvDB()
    encrypted_inputs: list[str] = []
    hinted_inputs: list[str] = []
    monkeypatch.setattr(
        plugin_runtime,
        "encrypt_secret",
        lambda value: encrypted_inputs.append(value) or "encrypted-fixture",
    )
    monkeypatch.setattr(
        plugin_runtime,
        "key_hint",
        lambda value: hinted_inputs.append(value) or "...04b",
    )

    with pytest.raises(RuntimeError) as raised:
        plugin_runtime.set_workbench_plugin_env_var(
            SimpleNamespace(db=database),
            project_id="project-04b",
            plugin_id="plugin.04b",
            name="PLUGIN_04B_TOKEN",
            value=raw_value,
        )

    assert raised.value is write_error
    _assert_raw_value_absent(raised.value, raw_value)
    assert encrypted_inputs == [raw_value]
    assert hinted_inputs == [raw_value]
    assert database.rollback_calls == 1
