"""Local speech libraries become usable without replacing the app interpreter."""

from pathlib import Path
import subprocess
import tomllib

import pytest

from frisket.runtime import model_install, speech_install
from frisket.runtime.launch import PythonRuntime, is_worker_argv, worker_argv


@pytest.mark.parametrize(
    "kind,engine",
    [
        ("parakeet-session", "parakeet-tdt"),
        ("parakeet-artifacts", "parakeet-tdt"),
        ("faster-whisper", "faster_whisper"),
    ],
)
def test_setup_changes_readiness_and_worker_interpreter_without_restart(
    monkeypatch, tmp_path, kind, engine
):
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(speech_install, "find_spec", lambda _module: None)
    assert not speech_install.runtime_present(engine)
    selected = speech_install.runtime_for_worker(kind)
    selected.executable.parent.mkdir(parents=True)
    selected.executable.write_text("")
    selected.executable.chmod(0o700)
    assert not speech_install.runtime_present(engine)
    (speech_install.runtime_dir() / ".ready").touch()
    assert speech_install.runtime_present(engine)
    assert speech_install.runtime_for_worker(kind).executable == selected.executable
    command = worker_argv(kind, runtime=selected)
    assert is_worker_argv(command)
    command[3] = "plugin"
    assert not is_worker_argv(command)
    command[3] = kind
    command[0] = str(tmp_path / "unowned-python")
    assert not is_worker_argv(command)


def test_existing_cli_runtime_is_preserved_per_engine(monkeypatch, tmp_path):
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        speech_install,
        "find_spec",
        lambda module: object() if module == "faster_whisper" else None,
    )
    assert speech_install.runtime_present("faster_whisper")
    assert not speech_install.runtime_present("parakeet-tdt")
    assert (
        speech_install.runtime_for_worker("faster-whisper") == PythonRuntime.current()
    )
    assert (
        speech_install.runtime_for_worker("parakeet-session").executable
        == speech_install.runtime_python()
    )


def test_speech_setup_uses_one_locked_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(
        model_install,
        "_install_profile",
        lambda profile, **kwargs: calls.append((profile, kwargs)),
    )

    def cancel():
        return False

    def progress(_message):
        pass

    speech_install.install_speech(should_cancel=cancel, progress=progress)
    profile, options = calls[0]
    assert profile.root == tmp_path / "speech-v1"
    assert profile.extra is None
    assert profile.requirements_file == "speech-requirements.txt"
    assert options == {"should_cancel": cancel, "progress": progress}


def test_speech_lock_has_only_worker_dependencies():
    root = Path(__file__).resolve().parents[2]
    lock = tomllib.loads((root / "sidecar/speech-runtime/uv.lock").read_text())
    names = {package["name"] for package in lock["package"]}
    assert {"faster-whisper", "onnx-asr", "pydantic", "av"} <= names
    assert (
        not {
            "frisket-data",
            "frisket-models",
            "sqlalchemy",
            "fastapi",
            "torch",
            "spacy",
        }
        & names
    )
    assert len(names) < 50


def test_speech_requirements_match_frozen_lock(tmp_path):
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / "requirements.txt"
    result = subprocess.run(
        [
            *model_install._uv_command(),
            "export",
            "--project",
            str(root / "sidecar/speech-runtime"),
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--output-file",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.returncode == 0

    # The generated command comment contains the caller's absolute paths.
    def lines(text):
        return [line for line in text.splitlines() if not line.startswith("#")]

    assert lines(output.read_text()) == lines(
        (root / "sidecar/speech-requirements.txt").read_text()
    )


def test_artifact_resolver_and_parakeet_protocol_import_without_host_dependencies(
    tmp_path,
):
    import os
    import venv

    root = Path(__file__).resolve().parents[2] / "src"
    environment = tmp_path / "empty"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from frisket.engine._workers import parakeet_artifacts, parakeet_artifacts_worker, parakeet_worker; "
            "assert 'frisket.engine.sandbox' not in sys.modules; "
            "assert 'frisket.sdk' not in sys.modules",
            str(root),
        ],
        env={},
        capture_output=True,
        text=True,
        check=True,
    )
