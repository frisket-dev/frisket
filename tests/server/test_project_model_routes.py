from __future__ import annotations

import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Event, local

from frisket.cloud_execution import CloudPayerSubject
from frisket.execution.credential_use import CredentialUseContext
from frisket.execution.provider import CompositionFacts, ExecutionComposition
from frisket.execution.targets import (
    CAPABILITY_LLM,
    CAPABILITY_OCR,
    ExecutionTarget,
    LLMOptionSupport,
    OcrOptionSupport,
    TargetEngineSupport,
)
from frisket.project_settings import (
    ProjectSettings,
    read_project_settings,
    write_project_settings,
)
from frisket.server.services.project_model_routes import (
    ProjectModelRouteError,
    ProjectModelRoutesService,
)
from frisket.server.workspace import Workspace

MODEL = "openai/gpt-5-mini"
MODEL_TWO = "openai/gpt-5-nano"


def _llm_target(
    target_id: str,
    *,
    cloud: bool,
    models: tuple[str, ...] = (MODEL,),
) -> ExecutionTarget:
    return ExecutionTarget(
        id=target_id,
        operator="frisket" if cloud else "openai",
        egress_class="frisket_shared" if cloud else "third_party_api",
        engines=tuple(
            TargetEngineSupport(
                engine=model,
                capability=CAPABILITY_LLM,
                transport="frisket.cloud.llm.v1" if cloud else "remote",
                options=LLMOptionSupport(tools=True),
            )
            for model in models
        ),
    )


class _Provider:
    def __init__(
        self,
        *,
        configured: frozenset[str] = frozenset(),
        cloud_first: bool = False,
        include_cloud: bool = True,
        local_only: bool = False,
        models: tuple[str, ...] = (MODEL,),
    ) -> None:
        if local_only:
            self._targets = (
                ExecutionTarget(
                    id="local",
                    operator="self",
                    egress_class="none",
                    engines=(
                        TargetEngineSupport(
                            engine="rapidocr",
                            capability=CAPABILITY_OCR,
                            transport="local",
                            options=OcrOptionSupport(language=True, geometry=True),
                        ),
                    ),
                ),
            )
        else:
            own = _llm_target("own", cloud=False, models=models)
            cloud = _llm_target("cloud", cloud=True, models=models)
            self._targets = (own, cloud) if include_cloud else (own,)
        self.configured = configured
        self.cloud_first = cloud_first
        self.connection_calls: list[str] = []

    def targets(self):
        return self._targets

    def configuration(self, target_id, capability, engine):
        del capability, engine
        return "configured" if target_id in self.configured else "not_configured"

    def selection_rank(self, target_id, capability, engine):
        del capability, engine
        if self.cloud_first:
            return 0 if target_id == "cloud" else 10
        return 0 if target_id != "cloud" else 10

    def connection(self, target_id):
        self.connection_calls.append(target_id)
        raise AssertionError("route pinning must not probe liveness")

    def cloud_payer(self, target_id, capability, engine):
        del capability
        if target_id != "cloud":
            return None
        return CloudPayerSubject(
            user_id=7,
            account_id=41,
            lane="frisket_credits",
            provider="openai",
            engine=engine,
            route_revision="route-1",
        )


def _workspace(
    tmp_path,
    monkeypatch,
    provider: _Provider,
    *,
    models: tuple[str, ...] = (MODEL,),
) -> Workspace:
    def composition(project, _router, _context):
        settings = read_project_settings(project)
        return ExecutionComposition(
            facts=CompositionFacts(),
            provider=provider,
            credential_use_context=CredentialUseContext.open(),
            model_routes=settings.model_execution_routes,
            model_route_default=settings.model_execution_default,
        )

    monkeypatch.setattr(
        "frisket.server.services.project_model_routes.build_provider_catalog",
        lambda *_args, **_kwargs: {
            "providers": [
                {
                    "id": "openai",
                    "kind": "platform_api",
                    "models": [{"id": model} for model in models],
                }
            ]
        },
    )
    return Workspace(
        tmp_path / "workspace",
        execution_composition_factory=composition,
    )


def test_new_projects_start_with_explicit_empty_model_routes(tmp_path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("New routes")["id"]

    settings = read_project_settings(workspace.get(project_id))

    assert settings.model_execution_routes == {}
    assert settings.model_execution_default is None


def test_pin_materializes_configured_blocked_choice_without_liveness_probe(
    tmp_path, monkeypatch
) -> None:
    provider = _Provider(
        configured=frozenset({"own", "cloud"}),
        cloud_first=True,
    )
    workspace = _workspace(tmp_path, monkeypatch, provider)
    project_id = workspace.create("Legacy Cloud first")["id"]
    write_project_settings(
        workspace.get(project_id),
        ProjectSettings(cloud_execution_preference="cloud_first"),
    )

    settings = ProjectModelRoutesService(workspace).pin_ready_routes(project_id)

    assert settings.model_execution_routes == {MODEL: "cloud"}
    assert settings.model_execution_default == "cloud"
    assert provider.connection_calls == []


def test_pin_leaves_unconfigured_model_unset(tmp_path, monkeypatch) -> None:
    provider = _Provider()
    workspace = _workspace(tmp_path, monkeypatch, provider)
    project_id = workspace.create("No configured routes")["id"]
    write_project_settings(workspace.get(project_id), ProjectSettings())

    settings = ProjectModelRoutesService(workspace).pin_ready_routes(project_id)

    assert settings.model_execution_routes == {}
    assert provider.connection_calls == []


def test_explicit_route_persists_before_setup_and_rejects_unknown_model(
    tmp_path, monkeypatch
) -> None:
    provider = _Provider()
    workspace = _workspace(tmp_path, monkeypatch, provider)
    project_id = workspace.create("Explicit Cloud setup")["id"]
    service = ProjectModelRoutesService(workspace)

    selected = service.set_route(project_id, MODEL, "cloud")

    assert selected.model_id == MODEL
    assert selected.route == "cloud"
    assert read_project_settings(workspace.get(project_id)).model_execution_routes == {
        MODEL: "cloud"
    }
    with pytest.raises(ProjectModelRouteError) as exc_info:
        service.set_route(project_id, "unknown/model", "own")
    assert exc_info.value.status == 422
    assert exc_info.value.code == "unknown_model"


def test_explicit_local_model_cannot_be_switched_to_cloud(
    tmp_path, monkeypatch
) -> None:
    provider = _Provider(local_only=True, configured=frozenset({"local"}))

    def composition(project, _router, _context):
        settings = read_project_settings(project)
        return ExecutionComposition(
            facts=CompositionFacts(),
            provider=provider,
            credential_use_context=CredentialUseContext.open(),
            model_routes=settings.model_execution_routes,
            model_route_default=settings.model_execution_default,
        )

    monkeypatch.setattr(
        "frisket.server.services.project_model_routes.build_provider_catalog",
        lambda *_args, **_kwargs: {"providers": []},
    )
    workspace = Workspace(
        tmp_path / "workspace",
        execution_composition_factory=composition,
    )
    project_id = workspace.create("Local OCR")["id"]

    with pytest.raises(ProjectModelRouteError) as exc_info:
        ProjectModelRoutesService(workspace).set_route(
            project_id,
            "rapidocr",
            "cloud",
        )

    assert exc_info.value.status == 409
    assert exc_info.value.code == "cloud_route_unsupported"


def test_concurrent_route_updates_merge_under_project_write_transaction(
    tmp_path, monkeypatch
) -> None:
    provider = _Provider(models=(MODEL, MODEL_TWO))
    workspace = _workspace(
        tmp_path,
        monkeypatch,
        provider,
        models=(MODEL, MODEL_TWO),
    )
    project_id = workspace.create("Concurrent routes")["id"]
    service = ProjectModelRoutesService(workspace)
    first_read = Event()
    first_written = Event()
    thread_state = local()
    original_read = read_project_settings

    def coordinated_read(project):
        settings = original_read(project)
        calls = getattr(thread_state, "calls", 0) + 1
        thread_state.calls = calls
        if calls == 1 and getattr(thread_state, "role", None) == "first":
            first_read.set()
            first_written.wait(timeout=0.2)
        elif calls == 1 and getattr(thread_state, "role", None) == "second":
            first_written.wait(timeout=1)
        return settings

    monkeypatch.setattr(
        "frisket.server.services.project_model_routes.read_project_settings",
        coordinated_read,
    )

    def select(model_id: str, role: str) -> None:
        thread_state.role = role
        if role == "second":
            assert first_read.wait(timeout=1)
        service.set_route(project_id, model_id, "own")
        if role == "first":
            first_written.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(select, MODEL, "first"),
            pool.submit(select, MODEL_TWO, "second"),
        ]
        for future in futures:
            future.result(timeout=2)

    assert read_project_settings(workspace.get(project_id)).model_execution_routes == {
        MODEL: "own",
        MODEL_TWO: "own",
    }
