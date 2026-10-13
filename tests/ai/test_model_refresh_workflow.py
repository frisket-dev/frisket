"""Public dispatch contract for the independent catalog refresh workflows."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _workflow(name: str) -> dict:
    path = ROOT / ".github" / "workflows" / name
    # rule19: parse the executable workflow manifest as GitHub Actions does.
    source = path.read_text(encoding="utf-8")
    return yaml.safe_load(source)


def _triggers(workflow: dict) -> dict:
    # PyYAML 1.1 parses the plain YAML key `on` as boolean true.
    return workflow.get("on", workflow.get(True))


def test_pricing_and_model_refreshes_have_independent_public_dispatches() -> None:
    pricing = _workflow("refresh-pricing.yml")
    models = _workflow("refresh-models.yml")

    assert pricing["jobs"] == {
        "refresh": {
            "uses": "./.github/workflows/refresh-catalog.yml",
            "with": {"mode": "pricing"},
        }
    }
    assert models["jobs"] == {
        "refresh": {
            "uses": "./.github/workflows/refresh-catalog.yml",
            "with": {"mode": "models"},
        }
    }
    assert _triggers(pricing) == {
        "schedule": [{"cron": "17 6 * * *"}],
        "workflow_dispatch": {},
    }
    assert _triggers(models) == {
        "schedule": [{"cron": "47 6 * * *"}],
        "workflow_dispatch": {},
    }
    assert pricing["concurrency"]["group"] != models["concurrency"]["group"]
