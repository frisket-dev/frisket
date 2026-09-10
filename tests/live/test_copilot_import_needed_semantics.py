from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.network, pytest.mark.live]

# The committed replay cassette: prompt/project-state -> recorded strict reply.
# Recorded by the live refresh step once the copilot wire contract ships; absent
# during red-first, which is a loud failure (not a skip).
CASSETTE_PATH = Path(__file__).parent / "cassettes" / "copilot_import_needed.json"

_MODEL_MODULE_CANDIDATES = (
    "frisket.contracts.http.models",
    "frisket.contracts.http.copilot",
    "frisket.contracts.http",
)

# Representative surfaces the model must classify (plan section 10).
REQUIRED_CASE_IDS = {
    "empty_project_csv_request",
    "empty_project_upload_request",
    "empty_project_url_request",
    "empty_project_youtube_request",
    "populated_project_actionable_request",
}


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _reply_model() -> Any | None:
    for module_name in _MODEL_MODULE_CANDIDATES:
        if not _module_available(module_name):
            continue
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        candidate = getattr(module, "CopilotReply", None)
        if candidate is not None:
            return candidate
    return None


def _load_cassette() -> list[dict[str, Any]] | None:
    if not CASSETTE_PATH.exists():
        return None
    try:
        payload = json.loads(CASSETTE_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    cases = payload.get("cases") if isinstance(payload, dict) else None
    return cases if isinstance(cases, list) else None


def test_copilot_import_need_is_model_decided_across_representative_surfaces() -> None:
    reply_model = _reply_model()
    assert reply_model is not None, (
        "CopilotReply wire model is absent — the Program G copilot import "
        "contract has not shipped, so the live/cassette semantic gate cannot run."
    )

    cases = _load_cassette()
    assert cases is not None, (
        f"copilot import-need semantic cassette is absent at {CASSETTE_PATH}. "
        "Record the live model replies for the representative CSV/upload/URL/"
        "YouTube/actionable surfaces before claiming this live gate green."
    )

    seen: set[str] = set()
    for case in cases:
        case_id = str(case.get("id", ""))
        seen.add(case_id)
        raw_reply = case.get("reply")
        assert isinstance(raw_reply, dict), (
            f"cassette case {case_id!r} has no recorded reply"
        )

        reply = reply_model.model_validate(raw_reply)
        dumped = reply.model_dump()
        needs_import = bool(dumped.get("needs_import"))
        proposals = dumped.get("proposals") or []

        if case.get("expect_needs_import"):
            assert needs_import, (
                f"case {case_id!r}: model failed to ask for an import on a data-less request"
            )
            assert proposals == [], (
                f"case {case_id!r}: an import-needed reply must carry no proposals"
            )
        else:
            assert not needs_import, (
                f"case {case_id!r}: model wrongly demanded an import for an actionable request"
            )
            assert proposals, (
                f"case {case_id!r}: an actionable reply must offer at least one proposal"
            )
            for proposal in proposals:
                kind = str(proposal.get("kind"))
                action_kind = str((proposal.get("spec") or {}).get("action_kind", ""))
                assert action_kind.split(".", 1)[0] == kind, (
                    f"case {case_id!r}: proposal discriminator {kind!r} disagrees "
                    f"with spec.action_kind {action_kind!r}"
                )

    missing = REQUIRED_CASE_IDS - seen
    assert not missing, (
        f"cassette is missing required representative cases: {sorted(missing)}"
    )
