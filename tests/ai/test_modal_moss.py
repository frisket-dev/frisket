from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.models import modal_moss


def _original(value: Any) -> Any:
    return next(
        (
            original
            for name, original in vars(value).items()
            if name.startswith("_sync_original_")
        ),
        value,
    )


def _function_spec(module: Any, name: str) -> tuple[Any, Any]:
    function = _original(module.app.registered_functions[name])
    return function._spec, function._webhook_config


def _named_secrets(spec: Any) -> set[str]:
    return {
        secret._name
        for secret in (_original(item) for item in spec.secrets)
        if secret._name is not None
    }


def _image_lineage(image: Any) -> list[str]:
    lineage: list[str] = []
    current = _original(image)
    while True:
        lineage.append(current._rep)
        dependencies = current._deps() if current._deps is not None else ()
        image_dependencies = [item for item in dependencies if hasattr(item, "_rep")]
        if not image_dependencies:
            return lineage
        assert len(image_dependencies) == 1
        current = _original(image_dependencies[0])


def test_moss_modal_runtime_image_id_is_namespaced() -> None:
    assert modal_moss.modal_runtime_image_id("im-Abc123") == "modal:im-Abc123"


def test_moss_modal_graph_uses_native_image_and_isolated_link_secret() -> None:
    pytest.importorskip("modal")
    from frisket.ai.models import modal_sidecar

    spec, webhook = _function_spec(modal_sidecar, "moss_worker")

    assert _original(spec.image) is _original(modal_moss.image)
    lineage = _image_lineage(spec.image)
    assert any(
        "from_dockerfile.<locals>.build_dockerfile_base" in item for item in lineage
    )
    assert all("from_registry" not in item for item in lineage)
    assert _named_secrets(spec) == {"frisket-moss-link"}
    assert (spec.gpus, spec.cpu, spec.memory) == ("A10", 2.0, 16384)
    assert webhook.web_server_port == 9000
    assert webhook.web_server_startup_timeout == 1800


def test_models_sidecar_separates_edge_config_and_moss_link_secrets() -> None:
    pytest.importorskip("modal")
    from frisket.ai.models import modal_sidecar

    spec, _webhook = _function_spec(modal_sidecar, "hosted_models")
    assert _original(spec.image) is _original(modal_sidecar.image)
    assert _named_secrets(spec) == {
        "frisket-dots-link",
        "frisket-models-edge",
        "frisket-moss-link",
    }
    assert (spec.gpus, spec.cpu, spec.memory) == ("T4", 4.0, 32768)
    assert set(spec.volumes) == {"/frisket-model-cache"}


def test_models_sidecar_selected_extras_match_the_checked_in_lock(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    requirements = tmp_path / "requirements.txt"
    result = subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--extra",
            "convert",
            "--extra",
            "ner",
            "--extra",
            "transcribe",
            "--output-file",
            str(requirements),
        ],
        cwd=root / "sidecar",
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    exported = requirements.read_text()
    assert "\npaddleocr==" not in exported
    assert "\npaddlepaddle==" not in exported


def test_models_sidecar_and_moss_share_one_modal_app() -> None:
    pytest.importorskip("modal")
    from frisket.ai.models import modal_sidecar

    assert set(modal_sidecar.app.registered_functions) == {
        "dots_mocr_worker",
        "glm_ocr_worker",
        "hosted_models",
        "moss_worker",
        "parakeet_worker",
        "paddleocr_vl_worker",
        "pp_ocrv6_worker",
    }


def test_models_sidecar_gets_moss_url_from_registered_function() -> None:
    from frisket.ai.models import modal_sidecar

    class MossWorker:
        def get_web_url(self) -> str:
            return "https://moss-worker.example.test"

    env = modal_sidecar.moss_worker_environment(MossWorker())

    assert env == {
        "FRISKET_TRANSCRIPTION_MOSS_WORKER_URL": ("https://moss-worker.example.test")
    }


def test_models_sidecar_sets_cache_paths_only_at_runtime() -> None:
    from frisket.ai.models import modal_sidecar

    assert modal_sidecar.model_cache_environment() == {
        "HF_HOME": "/frisket-model-cache/hf",
        "TORCH_HOME": "/frisket-model-cache/torch",
        "XDG_CACHE_HOME": "/frisket-model-cache/cache",
        "PADDLE_PDX_CACHE_HOME": "/frisket-model-cache/paddlex",
    }


@pytest.mark.parametrize(
    "image_id",
    [
        "",
        "im-",
        "modal:im-Abc123",
        "sha256:" + "a" * 64,
        "im-has space",
    ],
)
def test_moss_modal_rejects_missing_or_malformed_runtime_image_id(
    image_id: str,
) -> None:
    with pytest.raises(RuntimeError, match="MODAL_IMAGE_ID"):
        modal_moss.modal_runtime_image_id(image_id)
