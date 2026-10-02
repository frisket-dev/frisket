"""Run the required rolling-PR guard against real Git commits."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PRICE_PATH = "src/frisket/ai/llm/pricing_data.json"


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


@pytest.mark.parametrize("change", ["large-price", "invalid-price", "extra-code"])
def test_required_guard_rechecks_every_pr_head(tmp_path, change):
    workflow = yaml.safe_load(
        # rule19: execute the actual workflow artifact against disposable commits.
        (ROOT / ".github/workflows/deterministic-source.yml").read_text()
    )
    step = next(
        s
        for s in workflow["jobs"]["gate"]["steps"]
        if s.get("name") == "Validate rolling pricing PR"
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    for relative in [
        "scripts/dev/update_pricing.py",
        "src/frisket/ai/llm/model_catalog.json",
    ]:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    prices = repo / PRICE_PATH
    prices.write_text(json.dumps({"text": {"model": [1, 2]}, "audio": {}}))
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    prices.write_text(
        json.dumps(
            {
                "text": {"model": [-1 if change == "invalid-price" else 10000, 0]},
                "audio": {},
            }
        )
    )
    if change == "extra-code":
        (repo / "scripts/dev/update_pricing.py").write_text(
            "raise RuntimeError('must not execute modified validator')"
        )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "candidate")
    head = git(repo, "rev-parse", "HEAD")
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=repo,
        env={**os.environ, "BASE_SHA": base, "HEAD_SHA": head},
        capture_output=True,
        text=True,
        check=False,
    )
    if change == "large-price":
        assert result.returncode == 0, result.stderr
    elif change == "extra-code":
        assert result.returncode != 0
        assert "may change only" in result.stderr
        assert "must not execute" not in result.stderr
    else:
        assert result.returncode != 0
        assert "non-negative and finite" in result.stderr
