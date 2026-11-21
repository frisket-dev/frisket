from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "e2e" / "e2e_backend.sh"


def _run_backend_entrypoint(
    tmp_path: Path,
    *,
    minimal: bool,
    secrets_env: str | None = None,
    allow_live_spend: bool | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Run the launcher against a fake uv binary and return its argv and env."""

    fixture_root = tmp_path / "fixture"
    scripts = fixture_root / "scripts" / "e2e"
    bin_dir = tmp_path / "bin"
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    capture_args = tmp_path / "uv-args"
    capture_env = tmp_path / "uv-env"
    scripts.mkdir(parents=True)
    bin_dir.mkdir()
    home.mkdir()
    shutil.copy2(SCRIPT, scripts / SCRIPT.name)

    if secrets_env is not None:
        secrets_dir = fixture_root / ".secrets"
        secrets_dir.mkdir()
        (secrets_dir / "frisket.env").write_text(secrets_env)

    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$FRISKET_TEST_UV_ARGS"\n'
        'env > "$FRISKET_TEST_UV_ENV"\n'
    )
    uv.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FRISKET_E2E_WS": str(workspace),
        "FRISKET_E2E_BASE_WS": str(workspace),
        "FRISKET_E2E_BACKEND_PORT": "8123",
        "FRISKET_TEST_UV_ARGS": str(capture_args),
        "FRISKET_TEST_UV_ENV": str(capture_env),
    }
    for key in ("GEMINI_API_KEY", "OPENAI_API_KEY", "CENSUS_API_KEY", "FEC_API_KEY"):
        env.pop(key, None)
    if minimal:
        env["FRISKET_E2E_MINIMAL_DEPS"] = "1"
    if allow_live_spend is True:
        env["FRISKET_E2E_ALLOW_LIVE_SPEND"] = "1"
    elif allow_live_spend is False:
        env["FRISKET_E2E_ALLOW_LIVE_SPEND"] = "0"
    else:
        env.pop("FRISKET_E2E_ALLOW_LIVE_SPEND", None)
    if extra_env:
        env.update(extra_env)

    subprocess.run(
        ["bash", str(scripts / SCRIPT.name)],
        check=True,
        cwd=fixture_root,
        env=env,
    )
    parsed_env = dict(
        line.split("=", 1)
        for line in capture_env.read_text().splitlines()
        if "=" in line
    )
    return capture_args.read_text().splitlines(), parsed_env


def test_minimal_mode_uses_the_pre_synced_base_environment(tmp_path: Path) -> None:
    args, _env = _run_backend_entrypoint(tmp_path, minimal=True)
    assert args == [
        "run",
        "--no-sync",
        "frisket",
        str(tmp_path / "workspace"),
        "8123",
    ]


def test_default_mode_adds_entities_to_the_base_e2e_runtime(tmp_path: Path) -> None:
    args, _env = _run_backend_entrypoint(tmp_path, minimal=False)
    assert args == [
        "run",
        "--extra",
        "entities",
        "frisket",
        str(tmp_path / "workspace"),
        "8123",
    ]


def test_secrets_file_is_not_sourced_without_live_spend_opt_in(tmp_path: Path) -> None:
    _args, env = _run_backend_entrypoint(
        tmp_path,
        minimal=True,
        secrets_env="GEMINI_API_KEY=from-secrets\nOPENAI_API_KEY=openai-from-secrets\n",
    )
    assert "GEMINI_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env


def test_secrets_file_is_sourced_with_live_spend_opt_in_except_gated_keys(
    tmp_path: Path,
) -> None:
    _args, env = _run_backend_entrypoint(
        tmp_path,
        minimal=True,
        allow_live_spend=True,
        secrets_env=(
            "GEMINI_API_KEY=from-secrets\n"
            "CENSUS_API_KEY=census-secret\n"
            "FEC_API_KEY=fec-secret\n"
        ),
    )
    assert env.get("GEMINI_API_KEY") == "from-secrets"
    assert "CENSUS_API_KEY" not in env
    assert "FEC_API_KEY" not in env
