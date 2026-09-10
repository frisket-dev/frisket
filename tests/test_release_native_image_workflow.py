"""Executable contract for native release-image publication."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-public-artifacts.yml"
FINAL_DIGEST = "sha256:" + "f" * 64


def _workflow() -> dict:
    # rule19: this test executes and inspects the release workflow itself
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(job: str) -> dict[str, dict]:
    return {
        step["name"]: step
        for step in _workflow()["jobs"][job]["steps"]
        if "name" in step
    }


def test_release_builds_and_smokes_exactly_two_native_platforms() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["container-arch"]
    assert job["runs-on"] == "${{ matrix.runs_on }}"
    assert job["strategy"]["matrix"]["include"] == [
        {
            "platform": "linux/amd64",
            "tag_suffix": "amd64",
            "runs_on": "ubuntu-latest",
        },
        {
            "platform": "linux/arm64",
            "tag_suffix": "arm64",
            "runs_on": "ubuntu-24.04-arm",
        },
    ]
    # rule19: deletion fence supplements execution of the replacement scripts below
    assert "setup-qemu" not in WORKFLOW.read_text(encoding="utf-8")

    build = _steps("container-arch")["Build and push native OCI image by digest"]["run"]
    assert 'test "$PLATFORM" = "linux/$NATIVE_ARCH"' in build
    assert '--platform "$PLATFORM"' in build
    assert "linux/amd64,linux/arm64" not in build
    assert "--provenance=mode=max" in build
    assert "--sbom=true" in build
    assert "push-by-digest=true" in build
    assert build.index("docker pull --platform") < build.index("exiftool")
    assert build.index("exiftool") < build.index("smoke_media_metadata_runtime.py")
    assert build.index("smoke_media_metadata_runtime.py") < build.index(
        'touch "image-digest/'
    )

    container = workflow["jobs"]["container"]
    assert container["needs"] == ["build", "container-arch"]
    assert container["outputs"] == {
        "image": "${{ steps.publish.outputs.image }}",
        "digest": "${{ steps.publish.outputs.digest }}",
    }
    assert workflow["jobs"]["server-smoke"]["needs"] == ["build", "container"]
    assert workflow["jobs"]["publish"]["needs"] == [
        "build",
        "container",
        "server-smoke",
    ]


def _manifest(platforms: list[tuple[str, str]], *, attestation: bool = True) -> dict:
    manifests = [
        {
            "digest": f"sha256:{index:064x}",
            "platform": {"os": os_name, "architecture": arch},
        }
        for index, (os_name, arch) in enumerate(platforms, start=1)
    ]
    if attestation:
        manifests.append(
            {
                "digest": "sha256:" + "a" * 64,
                "platform": {"os": "unknown", "architecture": "unknown"},
                "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
            }
        )
    return {"schemaVersion": 2, "manifests": manifests}


def _install_fake_docker(fake_bin: Path) -> None:
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args and args[0] == "login":
    sys.stdin.read()
elif args[:3] == ["buildx", "imagetools", "create"]:
    Path(os.environ["DOCKER_CALLS"]).write_text(json.dumps(args), encoding="utf-8")
    metadata = Path(args[args.index("--metadata-file") + 1])
    metadata.write_text(json.dumps({"containerimage.descriptor": {"digest": os.environ["FINAL_DIGEST"]}}), encoding="utf-8")
elif args[:3] == ["buildx", "imagetools", "inspect"]:
    if "--raw" in args:
        ref = args[-1]
        fixture = (
            os.environ["FINAL_MANIFEST"]
            if ref.startswith("ghcr.io/frisket-dev/frisket@")
            else os.environ["EXTERNAL_MANIFEST"]
        )
        print(fixture)
    else:
        digest = (
            os.environ.get("VERSION_DIGEST", os.environ["FINAL_DIGEST"])
            if any(arg.endswith(":v1.2.3") for arg in args)
            else os.environ["FINAL_DIGEST"]
        )
        print(json.dumps({"digest": digest}))
elif args and args[0] == "pull":
    pass
else:
    raise SystemExit(f"unexpected docker invocation: {args}")
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)


def _run_assembly(
    tmp_path: Path,
    manifest: dict,
    *,
    digest_count: int = 2,
    version_digest: str | None = None,
) -> subprocess.CompletedProcess[str]:
    digest_dir = tmp_path / "image-digests"
    digest_dir.mkdir(parents=True)
    for index in range(digest_count):
        (digest_dir / f"{index + 1:064x}").touch()
    fake_bin = tmp_path / "bin"
    _install_fake_docker(fake_bin)
    step = _steps("container")["Assemble and validate immutable image manifest"]
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GH_TOKEN": "test-token",
        "COMMIT_SHA": "c" * 40,
        "VERSION": "1.2.3",
        "GITHUB_REPOSITORY": "frisket-dev/frisket",
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        "DOCKER_CALLS": str(tmp_path / "docker-calls.json"),
        "FINAL_DIGEST": FINAL_DIGEST,
        "FINAL_MANIFEST": json.dumps(manifest),
        "EXTERNAL_MANIFEST": json.dumps(
            _manifest([("linux", "amd64"), ("linux", "arm64")], attestation=False)
        ),
        "POSTGRES_IMAGE": "postgres@example",
        "CADDY_IMAGE": "caddy@example",
    }
    if version_digest is not None:
        env["VERSION_DIGEST"] = version_digest
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_assembly_publishes_both_tags_and_exports_the_validated_digest(
    tmp_path: Path,
) -> None:
    result = _run_assembly(
        tmp_path,
        _manifest([("linux", "amd64"), ("linux", "arm64")]),
    )
    assert result.returncode == 0, result.stderr
    call = json.loads((tmp_path / "docker-calls.json").read_text())
    assert call.count("--tag") == 2
    assert "ghcr.io/frisket-dev/frisket:sha-" + "c" * 40 in call
    assert "ghcr.io/frisket-dev/frisket:v1.2.3" in call
    assert sorted(arg for arg in call if "@sha256:" in arg) == [
        "ghcr.io/frisket-dev/frisket@sha256:" + f"{index:064x}" for index in (1, 2)
    ]
    assert (tmp_path / "github-output").read_text().splitlines() == [
        "image=ghcr.io/frisket-dev/frisket",
        f"digest={FINAL_DIGEST}",
    ]


@pytest.mark.parametrize(
    "platforms",
    [
        [("linux", "amd64")],
        [("linux", "amd64"), ("linux", "arm64"), ("linux", "s390x")],
        [("linux", "amd64"), ("windows", "arm64")],
    ],
)
def test_assembly_refuses_missing_or_extra_runtime_platforms(
    tmp_path: Path, platforms: list[tuple[str, str]]
) -> None:
    result = _run_assembly(tmp_path, _manifest(platforms))
    assert result.returncode != 0


def test_assembly_refuses_missing_digest_or_divergent_tags(tmp_path: Path) -> None:
    valid = _manifest([("linux", "amd64"), ("linux", "arm64")])
    missing = _run_assembly(tmp_path / "missing", valid, digest_count=1)
    assert missing.returncode != 0
    assert "expected exactly two native image digests" in missing.stdout

    divergent = _run_assembly(
        tmp_path / "divergent",
        valid,
        version_digest="sha256:" + "e" * 64,
    )
    assert divergent.returncode != 0
    assert "do not resolve to the same image" in divergent.stdout
