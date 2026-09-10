"""Executable contract for the bounded provider-neutral host installer."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "release" / "frisket-install"
DIGEST = "registry.example/frisket@sha256:" + "a" * 64
POSTGRES = "postgres@sha256:" + "b" * 64
CADDY = "caddy@sha256:" + "d" * 64


def _fake_docker(bin_dir: Path) -> None:
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        '[ -z "${FAKE_DOCKER_LOG:-}" ] || printf \'%s\\n\' "$*" >> "$FAKE_DOCKER_LOG"\n'
        'case "$1${2:+ $2}" in\n'
        "  'context show') echo default ;;\n"
        '  info*) [ "${FAKE_ROOTLESS:-0}" = 1 ] && echo rootless; exit 0 ;;\n'
        "  pull) exit 0 ;;\n"
        "  ps*)\n"
        '    case "$*" in\n'
        '      *network=*) [ -z "${FAKE_NETWORK_CONTAINERS:-}" ] || echo "$FAKE_NETWORK_CONTAINERS" ;;\n'
        '      *) [ "${FAKE_PROJECT_COLLISION:-0}" = 1 ] && echo container-id ;;\n'
        "    esac\n"
        "    exit 0 ;;\n"
        "  'network ls')\n"
        '    [ "${FAKE_NETWORK_LIST_FAIL:-0}" != 1 ] || exit 1\n'
        '    if [ "${FAKE_NETWORK_COLLISION:-0}" != 1 ]; then\n'
        "      network_triggered=false\n"
        '      if [ -n "${FAKE_NETWORK_AFTER_LOCK:-}" ] && [ -e "$FAKE_NETWORK_AFTER_LOCK" ]; then network_triggered=true; fi\n'
        '      [ "$network_triggered" = true ] || exit 0\n'
        "    fi\n"
        "    echo network-id ;;\n"
        "  'network inspect')\n"
        '    [ "${FAKE_NETWORK_INSPECT_FAIL:-0}" != 1 ] || exit 1\n'
        '    case "$4" in\n'
        "      '{{.Name}}') echo \"${FAKE_NETWORK_NAME:-frisket_default}\" ;;\n"
        '      *com.docker.compose.project*) echo "${FAKE_NETWORK_PROJECT-frisket}" ;;\n'
        '      *com.docker.compose.network*) echo "${FAKE_NETWORK_KIND-default}" ;;\n'
        "    esac ;;\n"
        "  inspect*)\n"
        '    case "$3" in\n'
        '      *project.working_dir*) [ -z "${FAKE_PROJECT_ROOT:-}" ] || echo "$FAKE_PROJECT_ROOT" ;;\n'
        '      *com.docker.compose.project*) echo "${FAKE_CONNECTED_PROJECT-frisket}" ;;\n'
        "    esac ;;\n"
        "  'compose version') echo 'Docker Compose version v2' ;;\n"
        "  compose*)\n"
        '    case "$*" in\n'
        '      *" exec "*) [ -z "${FAKE_MINT_TOKEN:-}" ] || printf \'%s\\n\' "$FAKE_MINT_TOKEN" ;;\n'
        "    esac\n"
        "    exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    uname = bin_dir / "uname"
    uname.write_text(
        "#!/bin/sh\n"
        'case "$1" in -s) echo Linux ;; -m) echo "${FAKE_ARCH:-x86_64}" ;; *) exit 1 ;; esac\n'
    )
    uname.chmod(0o755)
    (bin_dir / "flock").write_text('#!/bin/sh\n[ "${FAKE_FLOCK_FAIL:-0}" != 1 ]\n')
    (bin_dir / "flock").chmod(0o755)
    (bin_dir / "ss").write_text(
        "#!/bin/sh\n"
        '[ -n "${FAKE_BUSY_PORT:-}" ] && '
        "printf 'LISTEN 0 128 127.0.0.1:%s 0.0.0.0:*\\n' \"$FAKE_BUSY_PORT\"\n"
    )
    (bin_dir / "ss").chmod(0o755)
    (bin_dir / "curl").write_text(
        "#!/bin/sh\n"
        'case "${FAKE_CURL:-fail}:$*" in\n'
        "  success:*) exit 0 ;;\n"
        "  inner:*127.0.0.1:8000*) exit 0 ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    (bin_dir / "curl").chmod(0o755)


def _run(
    tmp_path: Path,
    *args: str,
    image: str = DIGEST,
    rootless: bool = False,
    ready_seconds: int = 0,
    extra_env: dict[str, str] | None = None,
    installer: Path = INSTALLER,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _fake_docker(bin_dir)
    root = tmp_path / "installed"
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FRISKET_INSTALL_ALLOW_TEST_ROOT": "1",
        "FRISKET_INSTALL_TEST_ROOT": str(root),
        "FRISKET_RELEASE_IMAGE": image,
        "FRISKET_RELEASE_POSTGRES_IMAGE": POSTGRES,
        "FRISKET_RELEASE_CADDY_IMAGE": CADDY,
        "FRISKET_INSTALL_READY_SECONDS": str(ready_seconds),
        "FAKE_ROOTLESS": "1" if rootless else "0",
    }
    env.update(extra_env or {})
    return subprocess.run(
        [str(installer), *args],
        text=True,
        capture_output=True,
        env=env,
        input=input_text,
    )


def test_installer_materializes_atomically_with_restrictive_env(tmp_path: Path) -> None:
    result = _run(tmp_path, "--tunnel", "--yes")
    assert result.returncode != 0  # readiness is intentionally forced to zero
    root = tmp_path / "installed"
    assert (root / "compose.yaml").is_file()
    assert (root / ".frisket-install").is_file()
    marker_text = (root / ".frisket-install").read_text()
    assert marker_text.startswith("format=3\n")
    assert "hash_frisket-deployment-diagnostics=" in marker_text
    assert stat.S_IMODE((root / ".env").stat().st_mode) == 0o600
    diagnostics = root / "frisket-deployment-diagnostics"
    assert diagnostics.is_file()
    assert stat.S_IMODE(diagnostics.stat().st_mode) == 0o755
    assert "FRISKET_IMAGE=" in (root / ".env").read_text()
    assert DIGEST not in result.stdout + result.stderr


def test_same_release_rerun_preserves_env_and_changed_release_refuses(
    tmp_path: Path,
) -> None:
    _run(tmp_path, "--tunnel", "--yes")
    env_path = tmp_path / "installed" / ".env"
    before = env_path.read_bytes()
    same = _run(tmp_path, "--tunnel", "--yes")
    assert same.returncode != 0
    assert env_path.read_bytes() == before
    owned = _run(
        tmp_path,
        "--tunnel",
        "--yes",
        extra_env={
            "FAKE_PROJECT_COLLISION": "1",
            "FAKE_PROJECT_ROOT": str(tmp_path / "installed"),
        },
    )
    assert "different Docker Compose project" not in owned.stderr
    assert env_path.read_bytes() == before
    changed = _run(
        tmp_path,
        "--tunnel",
        "--yes",
        image="registry.example/frisket@sha256:" + "c" * 64,
    )
    assert "rerun with --upgrade" in changed.stderr

    foreign = _run(
        tmp_path,
        "--tunnel",
        "--yes",
        extra_env={"FAKE_PROJECT_COLLISION": "1"},
    )
    assert "different Docker Compose project named frisket" in foreign.stderr


UPGRADE_DIGEST = "registry.example/frisket@sha256:" + "e" * 64


def test_upgrade_moves_the_release_and_preserves_operator_env(
    tmp_path: Path,
) -> None:
    _run(tmp_path, "--tunnel", "--yes")
    root = tmp_path / "installed"
    env_path = root / ".env"
    env_path.write_text(env_path.read_text() + "FRISKET_SMTP_HOST=smtp.example.org\n")

    upgraded = _run(
        tmp_path,
        "--upgrade",
        "--yes",
        image=UPGRADE_DIGEST,
        ready_seconds=3,
        extra_env={"FAKE_CURL": "success"},
    )

    assert upgraded.returncode == 0, upgraded.stderr
    # Selection is read from the marker; no access flags were passed.
    assert "selection: package=standalone access=tunnel" in upgraded.stderr
    assert (
        "upgraded: version=unknown package=standalone access=tunnel" in upgraded.stderr
    )
    assert f"image={UPGRADE_DIGEST}" in (root / ".frisket-install").read_text()
    env_text = env_path.read_text()
    assert f"FRISKET_IMAGE={UPGRADE_DIGEST}" in env_text
    assert env_text.count("FRISKET_IMAGE=") == 1
    assert "FRISKET_SMTP_HOST=smtp.example.org" in env_text
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    # An upgrade is not a fresh setup: no setup URL, no operator token mint.
    assert "Setup URL" not in upgraded.stderr
    assert "Operator token" not in upgraded.stdout
    # Digest pins never reach installer output in any mode.
    assert UPGRADE_DIGEST not in upgraded.stdout + upgraded.stderr

    # Same bundle again: nothing to move, plain rerun behavior.
    again = _run(
        tmp_path,
        "--upgrade",
        "--yes",
        image=UPGRADE_DIGEST,
        ready_seconds=3,
        extra_env={"FAKE_CURL": "success"},
    )
    assert "same release already materialized" in again.stderr


def test_interrupted_upgrade_is_rerunnable_not_fatal(tmp_path: Path) -> None:
    # Simulate an upgrade that died between refreshing generated files and
    # rewriting the marker: the tree's compose.yaml no longer matches the
    # marker hash. A rerun with --upgrade must repair, not refuse; without
    # --upgrade the integrity refusal stands.
    _run(tmp_path, "--tunnel", "--yes")
    root = tmp_path / "installed"
    compose = root / "compose.yaml"
    compose.write_text(compose.read_text() + "# half-finished upgrade\n")

    plain = _run(tmp_path, "--tunnel", "--yes")
    assert "generated file was modified or truncated: compose.yaml" in plain.stderr

    repaired = _run(
        tmp_path,
        "--upgrade",
        "--yes",
        image=UPGRADE_DIGEST,
        ready_seconds=3,
        extra_env={"FAKE_CURL": "success"},
    )
    assert repaired.returncode == 0, repaired.stderr
    assert "will be refreshed by the upgrade: compose.yaml" in repaired.stderr
    assert "# half-finished upgrade" not in compose.read_text()


def test_upgrade_refuses_missing_install_and_postgres_pin_change(
    tmp_path: Path,
) -> None:
    nothing = _run(tmp_path, "--upgrade", "--yes")
    assert nothing.returncode != 0
    assert "nothing to upgrade" in nothing.stderr

    _run(tmp_path, "--package", "multi-service", "--tunnel", "--yes")
    moved_postgres = _run(
        tmp_path,
        "--upgrade",
        "--yes",
        image=UPGRADE_DIGEST,
        extra_env={
            "FRISKET_RELEASE_POSTGRES_IMAGE": "postgres@sha256:" + "f" * 64,
        },
    )
    assert moved_postgres.returncode != 0
    assert "unsupported Postgres image change" in moved_postgres.stderr


def test_rerun_refuses_a_truncated_installation_marker(tmp_path: Path) -> None:
    _run(tmp_path, "--tunnel", "--yes")
    marker = tmp_path / "installed" / ".frisket-install"
    marker.write_text(
        "\n".join(
            line
            for line in marker.read_text().splitlines()
            if not line.startswith("hash_.env=")
        )
        + "\n"
    )

    check = _run(tmp_path, "--check", "--tunnel")
    rerun = _run(tmp_path, "--tunnel", "--yes")

    assert "installation marker is truncated" in check.stderr
    assert "preflight passed" not in check.stderr
    assert "installation marker is truncated" in rerun.stderr


def test_operator_edited_env_is_noted_and_kept_on_rerun(tmp_path: Path) -> None:
    # .env is operator-owned after install; an edit is configuration, not
    # corruption. Check and rerun both continue, note the divergence, and
    # never rewrite the operator's file. A *missing* .env stays fatal: the
    # generated secrets the stack depends on are gone.
    _run(tmp_path, "--tunnel", "--yes")
    env_path = tmp_path / "installed" / ".env"
    edited = env_path.read_text() + "FRISKET_SMTP_HOST=smtp.example.org\n"
    env_path.write_text(edited)

    check = _run(tmp_path, "--check", "--tunnel")
    rerun = _run(tmp_path, "--tunnel", "--yes")

    assert "preflight passed" in check.stderr
    assert "operator edits are expected" in check.stderr
    assert "modified or truncated" not in check.stderr + rerun.stderr
    assert "operator edits are expected" in rerun.stderr
    assert env_path.read_text() == edited

    env_path.unlink()
    gone = _run(tmp_path, "--check", "--tunnel")
    assert "generated file was modified or truncated: .env" in gone.stderr
    assert "preflight passed" not in gone.stderr


def test_rerun_refuses_modified_deployment_diagnostics(tmp_path: Path) -> None:
    _run(tmp_path, "--tunnel", "--yes")
    diagnostics = tmp_path / "installed" / "frisket-deployment-diagnostics"
    diagnostics.write_text(diagnostics.read_text() + "# modified\n")

    check = _run(tmp_path, "--check", "--tunnel")
    rerun = _run(tmp_path, "--tunnel", "--yes")

    assert (
        "generated file was modified or truncated: frisket-deployment-diagnostics"
        in check.stderr
    )
    assert "preflight passed" not in check.stderr
    assert (
        "generated file was modified or truncated: frisket-deployment-diagnostics"
        in rerun.stderr
    )


def test_check_refuses_non_executable_deployment_diagnostics(tmp_path: Path) -> None:
    _run(tmp_path, "--tunnel", "--yes")
    diagnostics = tmp_path / "installed" / "frisket-deployment-diagnostics"
    diagnostics.chmod(0o644)

    check = _run(tmp_path, "--check", "--tunnel")

    assert (
        "generated file was modified or truncated: frisket-deployment-diagnostics"
        in check.stderr
    )
    assert "preflight passed" not in check.stderr


def test_published_release_env_supplies_pins_without_operator_values(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    artifacts = bundle / "deploy" / "release"
    shutil.copytree(ROOT / "deploy" / "release", artifacts)
    bundled_installer = shutil.copy2(INSTALLER, bundle / "frisket-install")
    bundled_installer.chmod(0o755)
    shutil.copy2(
        ROOT / "scripts" / "release" / "frisket-deployment-diagnostics",
        bundle / "frisket-deployment-diagnostics",
    )
    bundled = "ghcr.io/example/frisket@sha256:" + "e" * 64
    (artifacts / "release.env").write_text(
        f"FRISKET_IMAGE={bundled}\nPOSTGRES_IMAGE={POSTGRES}\nCADDY_IMAGE={CADDY}\n"
    )
    _run(
        tmp_path / "host",
        "--tunnel",
        "--yes",
        image="",
        installer=bundled_installer,
    )
    installed_env = (tmp_path / "host" / "installed" / ".env").read_text()
    assert bundled in installed_env
    assert DIGEST not in installed_env


def test_invalid_origin_and_unmarked_collision_are_refused_before_install(
    tmp_path: Path,
) -> None:
    invalid = _run(tmp_path, "--behind-proxy", "https://bad.example/path", "--yes")
    assert invalid.returncode != 0
    assert not (tmp_path / "installed").exists()
    (tmp_path / "installed").mkdir()
    collision = _run(tmp_path, "--tunnel", "--yes")
    assert "target exists but is not a valid" in collision.stderr


def test_rerun_refuses_changed_origin_and_out_of_range_port(tmp_path: Path) -> None:
    _run(tmp_path, "--behind-proxy", "https://one.example.test", "--yes")
    changed = _run(
        tmp_path,
        "--behind-proxy",
        "https://two.example.test",
        "--yes",
    )
    assert "unsupported public-origin reconfiguration" in changed.stderr

    other = tmp_path / "other"
    invalid_port = _run(
        other,
        "--behind-proxy",
        "https://one.example.test:65536",
        "--yes",
    )
    assert "invalid HTTPS origin" in invalid_port.stderr
    assert not (other / "installed").exists()


def test_confirmation_and_rootless_daemon_refuse_before_materialization(
    tmp_path: Path,
) -> None:
    missing_confirmation = _run(tmp_path, "--tunnel")
    assert "selection: package=standalone access=tunnel" in missing_confirmation.stderr
    assert "rerun with --yes" in missing_confirmation.stderr
    assert not (tmp_path / "installed").exists()
    rootless = _run(tmp_path, "--tunnel", "--yes", rootless=True)
    assert "rootless Docker is unsupported" in rootless.stderr
    assert not (tmp_path / "installed").exists()


def test_check_defaults_to_recommended_path_and_never_mutates_host(
    tmp_path: Path,
) -> None:
    docker_log = tmp_path / "docker.log"

    result = _run(
        tmp_path,
        "--check",
        extra_env={"FAKE_DOCKER_LOG": str(docker_log)},
    )

    assert result.returncode == 0
    assert "selection: package=standalone access=tunnel" in result.stderr
    assert "preflight passed: host, release artifacts, Docker/Compose" in result.stderr
    assert "preflight made no changes" in result.stderr
    assert not (tmp_path / "installed").exists()
    assert not (tmp_path / ".frisket-install.lock").exists()
    assert not (tmp_path / ".frisket.staging").exists()
    docker_calls = docker_log.read_text().splitlines()
    assert not any(call.startswith("pull ") for call in docker_calls)
    assert not any(" up " in f" {call} " for call in docker_calls)


def test_check_aggregates_host_and_port_failures_without_mutation(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        "--check",
        rootless=True,
        extra_env={"FAKE_BUSY_PORT": "8000"},
    )

    assert result.returncode != 0
    assert "rootless Docker is unsupported" in result.stderr
    assert "port 8000 is already in use" in result.stderr
    assert "preflight found 2 issue(s); no changes made" in result.stderr
    assert not (tmp_path / "installed").exists()
    assert not (tmp_path / ".frisket-install.lock").exists()


def test_check_accepts_arm64_host(tmp_path: Path) -> None:
    result = _run(tmp_path, "--check", extra_env={"FAKE_ARCH": "aarch64"})

    assert result.returncode == 0
    assert "unsupported CPU architecture" not in result.stderr


def test_check_refuses_unsupported_cpu_architecture(tmp_path: Path) -> None:
    result = _run(tmp_path, "--check", extra_env={"FAKE_ARCH": "mips64"})

    assert result.returncode != 0
    assert (
        "unsupported CPU architecture 'mips64' (requires amd64 or arm64)"
        in result.stderr
    )


def test_check_accepts_explicit_multi_service_and_domain_selection(
    tmp_path: Path,
) -> None:
    package_only = _run(
        tmp_path / "package-only",
        "--check",
        "--package",
        "multi-service",
    )
    assert package_only.returncode == 0
    assert "package=multi-service access=tunnel" in package_only.stderr

    result = _run(
        tmp_path,
        "--check",
        "--package",
        "multi-service",
        "--domain",
        "Frisket.Example.Test",
    )

    assert result.returncode == 0
    assert (
        "selection: package=multi-service access=domain "
        "public_origin=https://frisket.example.test"
    ) in result.stderr
    assert "Install this configuration?" not in result.stderr
    assert not (tmp_path / "installed").exists()


def test_interactive_defaults_install_standalone_tunnel(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        ready_seconds=3,
        extra_env={"FAKE_CURL": "success"},
        input_text="\n\ny\n",
    )

    assert result.returncode == 0
    assert "Package [1=Standalone (recommended)" in result.stderr
    assert "Access [1=SSH tunnel (recommended)" in result.stderr
    assert "Install this configuration? [y/N]" in result.stderr
    assert "ready: version=unknown package=standalone access=tunnel" in result.stderr
    assert "Setup URL: http://localhost:8000/setup" in result.stderr
    assert (
        "FRISKET_BASE_URL=http://localhost:8000"
        in (tmp_path / "installed" / ".env").read_text()
    )


def test_interactive_domain_and_proxy_choices_are_bounded(tmp_path: Path) -> None:
    domain = _run(
        tmp_path / "domain",
        ready_seconds=3,
        extra_env={"FAKE_CURL": "success"},
        input_text="\n2\nFrisket.Example.Test\ny\n",
    )
    assert domain.returncode == 0
    assert "package=standalone access=domain" in domain.stderr
    assert "Setup URL: https://frisket.example.test/setup" in domain.stderr
    assert (tmp_path / "domain" / "installed" / "compose.edge.yaml").is_file()

    proxy = _run(
        tmp_path / "proxy",
        ready_seconds=3,
        extra_env={"FAKE_CURL": "success"},
        input_text="2\n3\nhttps://Proxy.Example.Test:8443\ny\n",
    )
    assert proxy.returncode == 0
    assert "package=multi-service access=behind-proxy" in proxy.stderr
    assert "Setup URL: https://proxy.example.test:8443/setup" in proxy.stderr
    installed_env = (tmp_path / "proxy" / "installed" / ".env").read_text()
    assert "FRISKET_TEAM_DATABASE_URL=" in installed_env
    assert "FRISKET_DOMAIN=" not in installed_env


def test_interactive_decline_and_eof_leave_no_installer_state(tmp_path: Path) -> None:
    declined = _run(tmp_path / "declined", input_text="\n\n\n")
    assert declined.returncode == 0
    assert "cancelled; no changes made" in declined.stderr
    assert not (tmp_path / "declined" / "installed").exists()
    assert not (tmp_path / "declined" / ".frisket-install.lock").exists()

    ended = _run(tmp_path / "ended", input_text="")
    assert ended.returncode != 0
    assert "interactive input ended" in ended.stderr
    assert not (tmp_path / "ended" / "installed").exists()


def test_success_handoff_is_exact_and_reports_bundle_version(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    artifacts = bundle / "deploy" / "release"
    shutil.copytree(ROOT / "deploy" / "release", artifacts)
    bundled_installer = shutil.copy2(INSTALLER, bundle / "frisket-install")
    bundled_installer.chmod(0o755)
    shutil.copy2(
        ROOT / "scripts" / "release" / "frisket-deployment-diagnostics",
        bundle / "frisket-deployment-diagnostics",
    )
    (bundle / "VERSION").write_text("9.8.7\n")
    (artifacts / "release.env").write_text(
        f"FRISKET_IMAGE={DIGEST}\nPOSTGRES_IMAGE={POSTGRES}\nCADDY_IMAGE={CADDY}\n"
    )

    mint_token = "frisket_operator_testtoken123"
    result = _run(
        tmp_path / "host",
        "--domain",
        "frisket.example.test",
        "--yes",
        image="",
        ready_seconds=3,
        extra_env={"FAKE_CURL": "success", "FAKE_MINT_TOKEN": mint_token},
        installer=bundled_installer,
    )

    assert result.returncode == 0
    installed = tmp_path / "host" / "installed"
    compose = "docker compose -p frisket -f compose.yaml -f compose.edge.yaml"
    assert "ready: version=9.8.7 package=standalone access=domain" in result.stderr
    assert "Setup URL: https://frisket.example.test/setup" in result.stderr
    # First-connection bootstrap: the minted token and pairing hint are
    # stdout-only, next to the server URL summary.
    assert f"Operator token (shown once): {mint_token}" in result.stdout
    assert (
        "Pair a workstation: frisket remote link https://frisket.example.test "
        f"--token {mint_token} --name frisket.example.test"
    ) in result.stdout
    assert mint_token not in result.stderr
    assert (
        f"Setup code: cd {installed} && {compose} logs --no-log-prefix app | "
        "grep '^FRISKET_SETUP_CODE ' | tail -n 1"
    ) in result.stderr
    assert f"Status: cd {installed} && {compose} ps" in result.stderr
    assert f"Logs: cd {installed} && {compose} logs --tail=100" in result.stderr
    assert (
        f"Diagnostics: cd {installed} && ./frisket-deployment-diagnostics"
        in result.stderr
    )
    assert DIGEST not in result.stdout + result.stderr
    # The token must never land in any installer-written file.
    for artifact in (".env", ".frisket-install"):
        assert mint_token not in (installed / artifact).read_text()


def test_success_without_container_mint_prints_the_exact_fallback_command(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        "--tunnel",
        "--yes",
        ready_seconds=3,
        extra_env={"FAKE_CURL": "success"},
    )
    assert result.returncode == 0
    installed = tmp_path / "installed"
    compose = "docker compose -p frisket -f compose.yaml"
    assert (
        "operator token not minted automatically; run: "
        f"cd {installed} && {compose} exec -T app frisket-control token mint --label initial"
    ) in result.stderr
    assert "Operator token (shown once):" not in result.stdout


def test_preexisting_project_network_refuses_before_installer_mutation(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        "--tunnel",
        "--yes",
        extra_env={"FAKE_NETWORK_COLLISION": "1"},
    )

    assert result.returncode != 0
    assert "different Docker Compose network for project frisket" in result.stderr
    assert not (tmp_path / "installed").exists()
    assert not (tmp_path / ".frisket-install.lock").exists()

    unlabeled = _run(
        tmp_path / "unlabeled",
        "--tunnel",
        "--yes",
        extra_env={
            "FAKE_NETWORK_COLLISION": "1",
            "FAKE_NETWORK_PROJECT": "",
            "FAKE_NETWORK_KIND": "",
        },
    )
    assert "different Docker Compose network for project frisket" in unlabeled.stderr
    assert not (tmp_path / "unlabeled" / "installed").exists()


def test_project_network_appearing_after_preflight_refuses_before_pull(
    tmp_path: Path,
) -> None:
    docker_log = tmp_path / "docker.log"
    lock = tmp_path / ".frisket-install.lock"

    result = _run(
        tmp_path,
        "--tunnel",
        "--yes",
        extra_env={
            "FAKE_DOCKER_LOG": str(docker_log),
            "FAKE_NETWORK_AFTER_LOCK": str(lock),
        },
    )

    assert result.returncode != 0
    assert "different Docker Compose network for project frisket" in result.stderr
    assert lock.is_file()
    assert not (tmp_path / "installed").exists()
    assert not any(
        call.startswith("pull ") for call in docker_log.read_text().splitlines()
    )


def test_only_marker_owned_project_network_is_accepted_on_retry(
    tmp_path: Path,
) -> None:
    _run(tmp_path, "--tunnel", "--yes")
    installed = tmp_path / "installed"
    network_only = _run(
        tmp_path,
        "--tunnel",
        "--yes",
        extra_env={"FAKE_NETWORK_COLLISION": "1"},
    )
    assert "different Docker Compose network" not in network_only.stderr
    assert "same release already materialized" in network_only.stderr

    owned_network = {
        "FAKE_NETWORK_COLLISION": "1",
        "FAKE_NETWORK_CONTAINERS": "connected-container",
        "FAKE_PROJECT_ROOT": str(installed),
    }

    owned = _run(tmp_path, "--tunnel", "--yes", extra_env=owned_network)
    assert "different Docker Compose network" not in owned.stderr
    assert "same release already materialized" in owned.stderr

    foreign = _run(
        tmp_path,
        "--check",
        "--tunnel",
        extra_env=owned_network | {"FAKE_PROJECT_ROOT": str(tmp_path / "foreign")},
    )
    assert "different Docker Compose network for project frisket" in foreign.stderr
    assert "preflight passed" not in foreign.stderr

    foreign_project = _run(
        tmp_path,
        "--check",
        "--tunnel",
        extra_env=owned_network | {"FAKE_CONNECTED_PROJECT": "other"},
    )
    assert (
        "different Docker Compose network for project frisket" in foreign_project.stderr
    )
    assert "preflight passed" not in foreign_project.stderr

    mislabeled = _run(
        tmp_path,
        "--check",
        "--tunnel",
        extra_env=owned_network | {"FAKE_NETWORK_KIND": "other"},
    )
    assert "different Docker Compose network for project frisket" in mislabeled.stderr
    assert "preflight passed" not in mislabeled.stderr


def test_lock_port_project_collision_and_stale_stage_are_bounded(
    tmp_path: Path,
) -> None:
    locked = _run(
        tmp_path / "locked",
        "--tunnel",
        "--yes",
        extra_env={"FAKE_FLOCK_FAIL": "1"},
    )
    assert "another Frisket installation" in locked.stderr

    busy = _run(
        tmp_path / "busy",
        "--tunnel",
        "--yes",
        extra_env={"FAKE_BUSY_PORT": "8000"},
    )
    assert "port 8000 is already in use" in busy.stderr

    collision = _run(
        tmp_path / "project",
        "--tunnel",
        "--yes",
        extra_env={"FAKE_PROJECT_COLLISION": "1"},
    )
    assert "Compose project named frisket" in collision.stderr

    interrupted = tmp_path / "interrupted"
    interrupted.mkdir()
    stage = interrupted / ".frisket.staging"
    stage.mkdir()
    (stage / ".frisket-stage").write_text("format=3\n")
    (stage / ".env").write_text("stale-secret\n")
    result = _run(interrupted, "--tunnel", "--yes")
    assert result.returncode != 0  # readiness remains forced off
    assert not stage.exists()
    assert (interrupted / "installed" / ".frisket-install").is_file()


def test_domain_pending_is_not_reported_as_success(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "--domain",
        "frisket.example.test",
        "--yes",
        ready_seconds=3,
        extra_env={"FAKE_CURL": "inner"},
    )
    assert result.returncode != 0
    assert "runtime ready, public edge pending" in result.stderr
