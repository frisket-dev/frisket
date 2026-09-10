from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from frisket.authoring.plugin_registry import (
    default_registry,
    register_trusted_backend_handler,
)
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status
from frisket.features.url_classification import registered_matchers
from test_workbench_plugin_generic_runtime_bindings import (
    ATOMIC_JOB_KIND,
    PLUGIN_ID,
    _activate_backend,
    _activate_manifest,
    _atomic_handler_keys,
    _atomic_runtime_bindings,
    _complete_publication,
    _publication_snapshot,
    loaded_atomic_plugin as _loaded_atomic_plugin,
)


@pytest.fixture()
def loaded_atomic_plugin(tmp_path: Any) -> Any:
    yield from _loaded_atomic_plugin.__wrapped__(tmp_path)


def _install_row(project: Any) -> tuple[int, str]:
    row = project.db.execute(
        "SELECT executable_handlers_allowed, updated_at "
        "FROM workbench_plugin_installs WHERE plugin_id=?",
        (PLUGIN_ID,),
    ).fetchone()
    assert row is not None
    return int(row["executable_handlers_allowed"]), str(row["updated_at"])


def test_failed_reactivation_restores_exact_prior_handlers(
    loaded_atomic_plugin: tuple[Any, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, project_id, receipt_id = loaded_atomic_plugin
    _activate_manifest(project, project_id, receipt_id)
    _activate_backend(project, project_id)
    registry = default_registry()
    prior_job = next(
        item for item in registry.job_handler_specs() if item.kind == ATOMIC_JOB_KIND
    )
    prior_bindings = {
        (binding_type, kind): next(
            item
            for item in registry.runtime_binding_specs(binding_type)
            if item.kind == kind
        )
        for binding_type, kind in _atomic_runtime_bindings()
    }
    prior_matchers = {
        item.matcher_id: item
        for item in registered_matchers()
        if item.origin_plugin_id == PLUGIN_ID
    }
    prior_install = _install_row(project)

    def replacement(payload: dict[str, Any]) -> dict[str, Any]:
        return {"replacement": True, "payload": payload}

    for key in _atomic_handler_keys():
        register_trusted_backend_handler(key, replacement, replace=True)

    register = registry.register_runtime_binding

    def fail_after_publish(*args: Any, **kwargs: Any) -> None:
        register(*args, **kwargs)
        raise RuntimeError("PRIMARY")

    monkeypatch.setattr(registry, "register_runtime_binding", fail_after_publish)
    with pytest.raises(RuntimeError, match="PRIMARY"):
        _activate_backend(project, project_id)

    current_job = next(
        item for item in registry.job_handler_specs() if item.kind == ATOMIC_JOB_KIND
    )
    assert current_job is prior_job
    for (binding_type, kind), prior in prior_bindings.items():
        current = next(
            item
            for item in registry.runtime_binding_specs(binding_type)
            if item.kind == kind
        )
        assert current is prior
    current_matchers = {
        item.matcher_id: item
        for item in registered_matchers()
        if item.origin_plugin_id == PLUGIN_ID
    }
    assert current_matchers == prior_matchers
    assert all(current_matchers[key] is prior_matchers[key] for key in prior_matchers)
    assert _install_row(project) == prior_install


def test_backend_activation_serializes_failure_before_following_success(
    loaded_atomic_plugin: tuple[Any, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, project_id, receipt_id = loaded_atomic_plugin
    activated = _activate_manifest(project, project_id, receipt_id)
    published = threading.Event()
    release = threading.Event()
    follower_started = threading.Event()
    follower_done = threading.Event()
    failures: list[BaseException] = []
    successes: list[dict[str, Any]] = []
    publish = plugin_runtime._register_backend_contribution_metadata

    def pause_failing_activation(*args: Any, **kwargs: Any) -> dict[str, list[str]]:
        result = publish(*args, **kwargs)
        if threading.current_thread().name == "failing-activation":
            published.set()
            assert release.wait(5)
            raise RuntimeError("PRIMARY")
        return result

    monkeypatch.setattr(
        plugin_runtime,
        "_register_backend_contribution_metadata",
        pause_failing_activation,
    )

    def fail() -> None:
        try:
            _activate_backend(project, project_id)
        except BaseException as exc:
            failures.append(exc)

    def follow() -> None:
        follower_started.set()
        successes.append(_activate_backend(project, project_id))
        follower_done.set()

    # realtime: Event-gated thread handshake, not a clock race
    failing = threading.Thread(target=fail, name="failing-activation")
    # realtime: Event-gated thread handshake, not a clock race
    follower = threading.Thread(target=follow, name="following-activation")
    failing.start()
    assert published.wait(5)
    follower.start()
    assert follower_started.wait(5)
    assert not follower_done.wait(0.1)
    release.set()
    failing.join(5)
    follower.join(5)

    assert not failing.is_alive()
    assert not follower.is_alive()
    assert len(failures) == 1 and str(failures[0]) == "PRIMARY"
    assert len(successes) == 1
    assert _publication_snapshot(project) == _complete_publication(activated)


def test_manifest_activation_serializes_failed_publisher_and_follower(
    loaded_atomic_plugin: tuple[Any, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, project_id, receipt_id = loaded_atomic_plugin
    published = threading.Event()
    release = threading.Event()
    follower_started = threading.Event()
    follower_done = threading.Event()
    failures: list[BaseException] = []
    successes: list[dict[str, Any]] = []
    upsert = plugin_runtime._upsert_workbench_plugin_install_state

    def pause_after_manifest(*args: Any, **kwargs: Any) -> None:
        if threading.current_thread().name == "failing-manifest":
            published.set()
            assert release.wait(5)
            raise RuntimeError("PRIMARY")
        upsert(*args, **kwargs)

    monkeypatch.setattr(
        plugin_runtime, "_upsert_workbench_plugin_install_state", pause_after_manifest
    )

    def fail() -> None:
        try:
            _activate_manifest(project, project_id, receipt_id)
        except BaseException as exc:
            failures.append(exc)

    def follow() -> None:
        follower_started.set()
        successes.append(_activate_manifest(project, project_id, receipt_id))
        follower_done.set()

    # realtime: Event-gated thread handshake, not a clock race
    failing = threading.Thread(target=fail, name="failing-manifest")
    # realtime: Event-gated thread handshake, not a clock race
    follower = threading.Thread(target=follow, name="following-manifest")
    failing.start()
    assert published.wait(5)
    follower.start()
    assert follower_started.wait(5)
    assert not follower_done.wait(0.1)
    release.set()
    failing.join(5)
    follower.join(5)

    assert not failing.is_alive()
    assert not follower.is_alive()
    assert len(failures) == 1 and str(failures[0]) == "PRIMARY"
    assert len(successes) == 1
    assert any(
        item.manifest.id == PLUGIN_ID for item in default_registry().plugin_manifests()
    )
    row = project.db.execute(
        "SELECT install_state FROM workbench_plugin_installs WHERE plugin_id=?",
        (PLUGIN_ID,),
    ).fetchone()
    assert row is not None and row["install_state"] == "enabled"


@pytest.mark.parametrize(
    ("lifecycle", "expected_state"),
    (
        (plugin_runtime.disable_workbench_plugin, "disabled"),
        (plugin_runtime.uninstall_workbench_plugin, "uninstalled"),
    ),
)
def test_backend_activation_serializes_lifecycle_unload(
    loaded_atomic_plugin: tuple[Any, str, str],
    monkeypatch: pytest.MonkeyPatch,
    lifecycle: Any,
    expected_state: str,
) -> None:
    project, project_id, receipt_id = loaded_atomic_plugin
    _activate_manifest(project, project_id, receipt_id)
    published = threading.Event()
    release = threading.Event()
    lifecycle_started = threading.Event()
    lifecycle_done = threading.Event()
    backend_results: list[dict[str, Any]] = []
    lifecycle_results: list[dict[str, Any]] = []
    publish = plugin_runtime._register_backend_contribution_metadata

    def pause_after_metadata(*args: Any, **kwargs: Any) -> dict[str, list[str]]:
        result = publish(*args, **kwargs)
        published.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(
        plugin_runtime,
        "_register_backend_contribution_metadata",
        pause_after_metadata,
    )

    def activate() -> None:
        backend_results.append(_activate_backend(project, project_id))

    def mutate() -> None:
        lifecycle_started.set()
        extra = (
            {"workspace_projects": (project,)}
            if lifecycle is plugin_runtime.uninstall_workbench_plugin
            else {}
        )
        lifecycle_results.append(
            lifecycle(
                project,
                project_id=project_id,
                plugin_id=PLUGIN_ID,
                **extra,
            )
        )
        lifecycle_done.set()

    # realtime: Event-gated thread handshake, not a clock race
    activating = threading.Thread(target=activate)
    # realtime: Event-gated thread handshake, not a clock race
    mutating = threading.Thread(target=mutate)
    activating.start()
    assert published.wait(5)
    mutating.start()
    assert lifecycle_started.wait(5)
    assert not lifecycle_done.wait(0.1)
    release.set()
    activating.join(5)
    mutating.join(5)

    assert not activating.is_alive()
    assert not mutating.is_alive()
    assert len(backend_results) == 1
    assert len(lifecycle_results) == 1
    snapshot = _publication_snapshot(project)
    install = next(item for item in snapshot if item[0] == "install_state")
    # The lifecycle transition serialized behind the activation lock and applied.
    assert install[4] == expected_state
    # Disable is project-local; uninstall deletes the workspace package and all
    # derived registry entries.
    if lifecycle is plugin_runtime.disable_workbench_plugin:
        assert ("manifest", PLUGIN_ID, install[2]) in snapshot
    else:
        assert not [item for item in snapshot if item[0] == "manifest"]
        assert not [item for item in snapshot if item[0] == "matcher"]
    assert not [item for item in snapshot if item[0] == "runtime_dispatch"]


def test_backend_failure_serializes_local_install_state_commit(
    loaded_atomic_plugin: tuple[Any, str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, project_id, receipt_id = loaded_atomic_plugin
    _activate_manifest(project, project_id, receipt_id)
    sentinel = "2001-02-03 04:05:06"
    project.db.execute(
        "UPDATE workbench_plugin_installs SET updated_at=? WHERE plugin_id=?",
        (sentinel, PLUGIN_ID),
    )
    project.db.commit()
    published = threading.Event()
    release = threading.Event()
    plugin_load_done = threading.Event()
    install_done = threading.Event()
    backend_failures: list[BaseException] = []
    install_failures: list[BaseException] = []
    install_results: list[tuple[int, dict[str, Any]]] = []
    publish = plugin_runtime._register_backend_contribution_metadata

    def pause_then_fail(*args: Any, **kwargs: Any) -> None:
        publish(*args, **kwargs)
        published.set()
        assert release.wait(5)
        raise RuntimeError("PRIMARY")

    monkeypatch.setattr(
        plugin_runtime,
        "_register_backend_contribution_metadata",
        pause_then_fail,
    )
    from frisket.engine import executor

    run_action_spec = executor.run_action_spec

    def track_plugin_load(*args: Any, **kwargs: Any) -> Any:
        result = run_action_spec(*args, **kwargs)
        plugin_load_done.set()
        return result

    monkeypatch.setattr(executor, "run_action_spec", track_plugin_load)

    def activate() -> None:
        try:
            _activate_backend(project, project_id)
        except BaseException as exc:
            backend_failures.append(exc)

    def install() -> None:
        try:
            install_results.append(
                plugin_runtime.execute_workbench_plugin_local_install_plan(
                    project,
                    project_id=project_id,
                    plugin_id=PLUGIN_ID,
                    source={
                        "kind": "localPath",
                        "value": str(
                            tmp_path
                            / "plugin_packages"
                            / "activation-atomicity"
                            / "plugin.json"
                        ),
                    },
                    arbitrary_package_load_allowed=False,
                )
            )
        except BaseException as exc:
            install_failures.append(exc)
        finally:
            install_done.set()

    # realtime: Event-gated thread handshake, not a clock race
    activating = threading.Thread(target=activate)
    # realtime: Event-gated thread handshake, not a clock race
    installing = threading.Thread(target=install)
    activating.start()
    assert published.wait(5)
    installing.start()
    # Package validation and its catalog commit share one lifecycle boundary;
    # an install may not stage through plugin.load while backend publication is
    # still inside that boundary.
    assert not plugin_load_done.wait(0.1)
    assert not install_done.wait(0.1)
    release.set()
    activating.join(5)
    installing.join(5)

    assert not activating.is_alive()
    assert not installing.is_alive()
    assert plugin_load_done.is_set()
    assert len(backend_failures) == 1 and str(backend_failures[0]) == "PRIMARY"
    assert install_failures == []
    assert len(install_results) == 1 and install_results[0][0] == 200
    row = project.db.execute(
        "SELECT install_state, activation, updated_at "
        "FROM workbench_plugin_installs WHERE plugin_id=?",
        (PLUGIN_ID,),
    ).fetchone()
    assert row is not None
    assert (row["install_state"], row["activation"]) == (
        "installed",
        "manifestLoaded",
    )
    assert row["updated_at"] != sentinel


def test_cleanup_failures_do_not_mask_primary_or_skip_later_cleanup(
    loaded_atomic_plugin: tuple[Any, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, project_id, receipt_id = loaded_atomic_plugin
    _activate_manifest(project, project_id, receipt_id)
    calls: list[str] = []

    def primary(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("PRIMARY")

    def cleanup_failure(*_args: Any, **_kwargs: Any) -> None:
        calls.append("grant")
        raise RuntimeError("CLEANUP")

    def contribution_cleanup(*_args: Any, **_kwargs: Any) -> None:
        calls.append("contributions")

    registry = default_registry()
    restore_manifest = registry._restore_plugin_manifest

    def manifest_cleanup(*args: Any, **kwargs: Any) -> None:
        calls.append("manifest")
        restore_manifest(*args, **kwargs)

    monkeypatch.setattr(
        plugin_runtime, "_register_backend_contribution_metadata", primary
    )
    monkeypatch.setattr(
        plugin_runtime_status,
        "_restore_backend_activation_executable_handlers_grant",
        cleanup_failure,
    )
    monkeypatch.setattr(
        plugin_runtime, "_restore_backend_contributions", contribution_cleanup
    )
    monkeypatch.setattr(registry, "_restore_plugin_manifest", manifest_cleanup)

    with pytest.raises(RuntimeError, match="PRIMARY") as caught:
        _activate_backend(project, project_id)

    assert calls == ["grant", "contributions", "manifest"]
    assert any("CLEANUP" in note for note in getattr(caught.value, "__notes__", ()))


def test_grant_rollback_restores_value_and_timestamp_exactly(
    loaded_atomic_plugin: tuple[Any, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, project_id, receipt_id = loaded_atomic_plugin
    _activate_manifest(project, project_id, receipt_id)
    project.db.execute(
        "UPDATE workbench_plugin_installs "
        "SET executable_handlers_allowed=0, updated_at='2001-02-03 04:05:06' "
        "WHERE plugin_id=?",
        (PLUGIN_ID,),
    )
    project.db.commit()
    prior = _install_row(project)

    plugin_runtime.activate_workbench_plugin_backend_contributions(
        project,
        project_id=project_id,
        plugin_id=PLUGIN_ID,
        trust_acknowledged=True,
        arbitrary_package_load_allowed=False,
        executable_handlers_allowed=False,
    )
    assert _install_row(project) == prior

    record = plugin_runtime._record_backend_activation_executable_handlers_grant

    def fail_after_commit(*args: Any, **kwargs: Any) -> None:
        record(*args, **kwargs)
        raise RuntimeError("PRIMARY")

    monkeypatch.setattr(
        plugin_runtime,
        "_record_backend_activation_executable_handlers_grant",
        fail_after_commit,
    )
    with pytest.raises(RuntimeError, match="PRIMARY"):
        _activate_backend(project, project_id)

    assert _install_row(project) == prior
