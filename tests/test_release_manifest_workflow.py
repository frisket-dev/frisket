"""Executable contract for the public release manifest transaction."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-public-artifacts.yml"
REPOSITORY = "frisket-dev/frisket"
RELEASE_ID = 456
VERSION = "0.1.1a99"
TAG = f"v{VERSION}"
SOURCE_SHA = "a" * 40
IMAGE_REPOSITORY = "ghcr.io/frisket-dev/frisket"
IMAGE_DIGEST = "sha256:" + "b" * 64


def _publish_steps() -> dict[str, dict]:
    # rule19: each selected workflow shell artifact is executed by these tests
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return {
        step["name"]: step
        for step in workflow["jobs"]["publish"]["steps"]
        if "name" in step
    }


def _install_fake_gh(fake_bin: Path) -> None:
    fake_bin.mkdir()
    gh = fake_bin / "gh"
    gh.write_text(
        """#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

args = sys.argv[1:]
state_path = Path(os.environ["GH_RELEASE_STATE"])
asset_dir = Path(os.environ["GH_ASSET_DIR"])
state = json.loads(state_path.read_text(encoding="utf-8"))

if args[0] == "api":
    endpoint = args[1]
    if endpoint.endswith(f"/releases/{state['id']}"):
        print(json.dumps(state))
    elif "/releases/assets/" in endpoint:
        asset_id = int(endpoint.rsplit("/", 1)[1])
        sys.stdout.buffer.write((asset_dir / str(asset_id)).read_bytes())
    else:
        raise SystemExit(f"unexpected gh api endpoint: {endpoint}")
elif args[:2] == ["release", "upload"]:
    source = Path(args[3])
    asset_id = max([asset["id"] for asset in state["assets"]], default=1000) + 1
    contents = source.read_bytes()
    state["assets"].append({
        "name": source.name,
        "id": asset_id,
        "size": len(contents),
        "browser_download_url": (
            f"https://github.com/{os.environ['TEST_REPOSITORY']}/releases/"
            f"download/{state['tag_name']}/{source.name}"
        ),
    })
    state_path.write_text(json.dumps(state), encoding="utf-8")
    shutil.copyfile(source, asset_dir / str(asset_id))
else:
    raise SystemExit(f"unexpected gh invocation: {args}")
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    release_assets = tmp_path / "release-assets"
    runner_temp = tmp_path / "runner-temp"
    fake_bin = tmp_path / "bin"
    asset_dir = tmp_path / "remote-assets"
    for directory in (release_assets, runner_temp, asset_dir):
        directory.mkdir()

    payloads = {
        f"frisket-{VERSION}-py3-none-any.whl": b"wheel-payload",
        f"frisket-server-{VERSION}.tar.gz": b"server-payload",
        f"frisket-web-team-{VERSION}.tar.gz": b"frontend-payload",
        "frisket-release-pins.env": b"FRISKET_IMAGE=test\n",
        "SHA256SUMS": b"payload checksums\n",
    }
    assets = []
    for offset, (name, contents) in enumerate(payloads.items(), start=1):
        local = release_assets / name
        local.write_bytes(contents)
        asset_id = 1000 + offset
        (asset_dir / str(asset_id)).write_bytes(contents)
        assets.append(
            {
                "name": name,
                "id": asset_id,
                "size": len(contents),
                "browser_download_url": (
                    f"https://github.com/{REPOSITORY}/releases/download/"
                    f"untagged-draft/{name}"
                ),
            }
        )

    state = {
        "id": RELEASE_ID,
        "tag_name": TAG,
        "target_commitish": SOURCE_SHA,
        "draft": True,
        "prerelease": True,
        "assets": assets,
    }
    state_path = tmp_path / "release.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    _install_fake_gh(fake_bin)
    return release_assets, state_path, fake_bin


def _run_step(
    step_name: str,
    *,
    release_assets: Path,
    state_path: Path,
    fake_bin: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    script = _publish_steps()[step_name]["run"].replace(
        "${{ github.repository }}", REPOSITORY
    )
    runner_temp = release_assets.parent / "runner-temp"
    output = runner_temp / "github-output"
    summary = runner_temp / "github-summary"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GH_RELEASE_STATE": str(state_path),
        "GH_ASSET_DIR": str(release_assets.parent / "remote-assets"),
        "TEST_REPOSITORY": REPOSITORY,
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
        "RELEASE_ID": str(RELEASE_ID),
        "TAG": TAG,
        "VERSION": VERSION,
        "TARGET": SOURCE_SHA,
        "PRERELEASE": "true",
        "IMAGE_REPOSITORY": IMAGE_REPOSITORY,
        "IMAGE_DIGEST": IMAGE_DIGEST,
    }
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=release_assets,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def test_manifest_is_deterministic_and_derives_final_tag_payload_urls(
    tmp_path: Path,
) -> None:
    release_assets, state_path, fake_bin = _fixture(tmp_path)

    _run_step(
        "Generate resolved release manifest",
        release_assets=release_assets,
        state_path=state_path,
        fake_bin=fake_bin,
    )
    manifest_path = release_assets / "frisket-release.json"
    first = manifest_path.read_bytes()
    _run_step(
        "Generate resolved release manifest",
        release_assets=release_assets,
        state_path=state_path,
        fake_bin=fake_bin,
    )
    assert manifest_path.read_bytes() == first

    manifest = json.loads(first)
    assert manifest == {
        "payloads": sorted(
            [
                {
                    "browser_download_url": (
                        f"https://github.com/{REPOSITORY}/releases/download/"
                        f"{TAG}/{asset['name']}"
                    ),
                    "id": asset["id"],
                    "name": asset["name"],
                    "path": f"/repos/{REPOSITORY}/releases/assets/{asset['id']}",
                    "sha256": hashlib.sha256(
                        (release_assets / asset["name"]).read_bytes()
                    ).hexdigest(),
                    "size": asset["size"],
                }
                for asset in json.loads(state_path.read_text())["assets"]
            ],
            key=lambda asset: asset["name"],
        ),
        "release": {
            "id": RELEASE_ID,
            "path": f"/repos/{REPOSITORY}/releases/{RELEASE_ID}",
            "prerelease": True,
            "repository": REPOSITORY,
            "tag": TAG,
            "version": VERSION,
        },
        "schema_version": "frisket.release.v1",
        "server_image": {
            "digest": IMAGE_DIGEST,
            "reference": f"{IMAGE_REPOSITORY}@{IMAGE_DIGEST}",
            "repository": IMAGE_REPOSITORY,
        },
        "source": {"sha": SOURCE_SHA},
    }
    assert "frisket-release.json" not in {
        payload["name"] for payload in manifest["payloads"]
    }
    assert all(
        "/untagged-" not in payload["browser_download_url"]
        for payload in manifest["payloads"]
    )


def test_manifest_upload_recovers_only_identical_bytes_and_records_own_digest(
    tmp_path: Path,
) -> None:
    release_assets, state_path, fake_bin = _fixture(tmp_path)
    _run_step(
        "Generate resolved release manifest",
        release_assets=release_assets,
        state_path=state_path,
        fake_bin=fake_bin,
    )
    _run_step(
        "Upload or recover release manifest last",
        release_assets=release_assets,
        state_path=state_path,
        fake_bin=fake_bin,
    )
    state = json.loads(state_path.read_text())
    assert [asset["name"] for asset in state["assets"]].count(
        "frisket-release.json"
    ) == 1

    # A rerun starts at payload recovery. The manifest uploaded last by the
    # interrupted run is a known name, but every payload is still reverified.
    _run_step(
        "Upload or recover payload assets",
        release_assets=release_assets,
        state_path=state_path,
        fake_bin=fake_bin,
    )
    before_recovery = (release_assets / "frisket-release.json").read_bytes()
    _run_step(
        "Generate resolved release manifest",
        release_assets=release_assets,
        state_path=state_path,
        fake_bin=fake_bin,
    )
    assert (release_assets / "frisket-release.json").read_bytes() == before_recovery

    recovered = _run_step(
        "Upload or recover release manifest last",
        release_assets=release_assets,
        state_path=state_path,
        fake_bin=fake_bin,
    )
    assert "retaining byte-identical draft manifest" in recovered.stdout

    _run_step(
        "Verify uploaded release manifest and record external digest",
        release_assets=release_assets,
        state_path=state_path,
        fake_bin=fake_bin,
    )
    expected = hashlib.sha256(
        (release_assets / "frisket-release.json").read_bytes()
    ).hexdigest()
    output = (release_assets.parent / "runner-temp" / "github-output").read_text()
    summary = (release_assets.parent / "runner-temp" / "github-summary").read_text()
    assert f"sha256={expected}" in output
    assert expected in summary

    (release_assets / "frisket-release.json").write_text("{}\n", encoding="utf-8")
    mismatch = _run_step(
        "Upload or recover release manifest last",
        release_assets=release_assets,
        state_path=state_path,
        fake_bin=fake_bin,
        check=False,
    )
    assert mismatch.returncode != 0
    assert "differs from resolved release facts" in mismatch.stdout


def test_workflow_orders_manifest_after_payload_verification_and_before_publish() -> (
    None
):
    steps = _publish_steps()
    names = list(steps)
    assert names.index(
        "Re-verify uploaded asset sha256 via the release-assets API"
    ) < names.index("Generate resolved release manifest")
    assert names.index("Generate resolved release manifest") < names.index(
        "Upload or recover release manifest last"
    )
    assert names.index(
        "Verify uploaded release manifest and record external digest"
    ) < names.index("Publish release (assets become immutable)")
    payload_upload = steps["Upload or recover payload assets"]["run"]
    assert "gh release upload" in payload_upload
    assert '[ "$remote_name" != frisket-release.json ] || continue' in payload_upload
    assert (
        "frisket-release.json"
        not in steps["Re-verify uploaded asset sha256 via the release-assets API"][
            "run"
        ]
    )
    assert (
        "frisket-release.json"
        in steps["Verify published assets are anonymously downloadable when public"][
            "run"
        ]
    )
