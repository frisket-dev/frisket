from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release" / "frisket-deployment-diagnostics"


def _fake_docker(bin_dir: Path) -> None:
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  ps) printf '%s\\n' app-id worker-id provision-id migrate-id ;;\n"
        "  inspect)\n"
        "    fmt=$3; id=$4\n"
        '    case "$fmt:$id" in\n'
        "      *com.docker.compose.service*app-id) echo app ;;\n"
        "      *com.docker.compose.service*worker-id) echo worker ;;\n"
        "      *com.docker.compose.service*provision-id) echo run-queue-provision ;;\n"
        "      *com.docker.compose.service*migrate-id) echo run-queue-migrate ;;\n"
        "      *State.Status*app-id|*State.Status*worker-id) echo running ;;\n"
        "      *State.Status*provision-id|*State.Status*migrate-id) echo exited ;;\n"
        "      *State.ExitCode*provision-id|*State.ExitCode*migrate-id) echo 0 ;;\n"
        "      *State.Health*app-id) echo healthy ;;\n"
        "      *State.Health*worker-id) echo none ;;\n"
        "      *Config.Image*app-id) echo ghcr.io/frisket-dev/frisket@sha256:app ;;\n"
        "      *Config.Image*worker-id) echo ghcr.io/frisket-dev/frisket@sha256:worker ;;\n"
        "      *Config.Image*provision-id|*Config.Image*migrate-id) echo ghcr.io/frisket-dev/frisket@sha256:jobs ;;\n"
        "      *.Image*app-id) echo sha256:app-image ;;\n"
        "      *.Image*worker-id) echo sha256:worker-image ;;\n"
        "      *.Image*provision-id|*.Image*migrate-id) echo sha256:jobs-image ;;\n"
        "      *) echo unavailable ;;\n"
        "    esac ;;\n"
        "  port) echo '8000/tcp -> 127.0.0.1:8000' ;;\n"
        "  logs)\n"
        "    echo 'FRISKET_SETUP_CODE setup-secret /setup'\n"
        "    echo 'Authorization: Bearer bearer-secret'\n"
        "    echo 'postgresql://frisket:database-secret@db:5432/frisket'\n"
        '    echo \'{"token":"json-token-secret"}\' ;;\n'
        "esac\n"
    )
    docker.chmod(0o755)

    curl = bin_dir / "curl"
    curl.write_text("#!/bin/sh\nprintf '%s\\n' '{\"ok\":true}' 'http_status=200'\n")
    curl.chmod(0o755)


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_docker(bin_dir)
    root = tmp_path / "frisket"
    (root / "data").mkdir(parents=True)
    (root / "postgres").mkdir()
    (root / ".env").write_text("POSTGRES_PASSWORD=env-file-secret\n")
    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return subprocess.run(
        [str(SCRIPT), "--root", str(root), *args],
        text=True,
        capture_output=True,
        env=env,
    )


def test_collector_reports_useful_partial_stack_evidence_and_redacts_logs(
    tmp_path: Path,
) -> None:
    output = tmp_path / "diagnostics.txt"
    proc = _run(tmp_path, "--tail", "2", "--output", str(output))

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    report = output.read_text()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "schema=frisket.deployment-diagnostics.v1" in report
    assert "CONTAINER|service=app|id=app-id|state=running|health=healthy" in report
    assert "image=ghcr.io/frisket-dev/frisket@sha256:app" in report
    assert "ONE_SHOT|service=run-queue-provision|state=exited|exit_code=0" in report
    assert "READINESS|url=http://127.0.0.1:8000/api/ready|curl_exit=0" in report
    assert "LOG|service=app|tail=2" in report
    assert "FRISKET_SETUP_CODE [redacted] /setup" in report
    assert "Bearer [redacted]" in report
    assert "postgresql://[redacted]" in report
    for secret in (
        "setup-secret",
        "bearer-secret",
        "database-secret",
        "json-token-secret",
        "env-file-secret",
    ):
        assert secret not in report
    assert "collector_status=complete" in report


def test_collector_is_usable_when_the_stack_is_missing(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "docker").chmod(0o755)
    (bin_dir / "curl").write_text("#!/bin/sh\nexit 7\n")
    (bin_dir / "curl").chmod(0o755)
    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    proc = subprocess.run(
        [str(SCRIPT), "--root", str(tmp_path / "missing")],
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    assert "CONTAINERS|none found" in proc.stdout
    assert "ONE_SHOT|service=run-queue-provision|missing" in proc.stdout
    assert "LOG|service=app|missing" in proc.stdout
    assert "collector_status=complete" in proc.stdout


def test_collector_refuses_to_overwrite_a_report_and_never_invokes_compose(
    tmp_path: Path,
) -> None:
    output = tmp_path / "existing.txt"
    output.write_text("keep")
    proc = _run(tmp_path, "--output", str(output))

    assert proc.returncode == 2
    assert output.read_text() == "keep"
    # rule19: executes: this test runs the script via _run; the source check is supplementary
    source = SCRIPT.read_text()
    assert "docker compose" not in source
    assert 'cat "$ROOT/.env"' not in source


def test_collector_refuses_a_dangling_output_symlink(tmp_path: Path) -> None:
    target = tmp_path / "must-not-be-created.txt"
    output = tmp_path / "diagnostics.txt"
    output.symlink_to(target)

    proc = _run(tmp_path, "--output", str(output))

    assert proc.returncode == 2
    assert not target.exists()
