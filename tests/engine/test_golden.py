"""Golden project #7: classify-into-categories — the simplest
golden, so the core loop is tested end-to-end before anything fancy exists.

Runs from the committed cache by default (replay_strict, keyless). To refresh:
    FRISKET_CACHE_REFRESH=1 FRISKET_CACHE_MODE=replay pytest tests/engine/test_golden.py

Recording requires GEMINI_API_KEY and explicitly echoes the runner's cost gate.
"""

import asyncio
import os

import pytest

from frisket.ai.llm import ModelRouter, ResponseCache
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.testing import llm_cache_path
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from typed_model_fixtures import estimate_model_run, run_with_exact_confirmation

CACHE_PATH = llm_cache_path()
MODE = os.environ.get("FRISKET_CACHE_MODE", "replay_strict")


@pytest.fixture(scope="module")
def golden_model_keys():
    # Capture only explicitly requested recording credentials before the
    # per-test hermetic environment strips ambient provider keys.
    if os.environ.get("FRISKET_CACHE_REFRESH") != "1":
        return {}
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        pytest.fail("Golden cache recording requires GEMINI_API_KEY")
    return {"gemini": key}


# Hand-labeled golden subset: known-answer rows (the "tip line" property).
SNIPPETS = [
    (
        "The mayor's office quietly awarded a $4M paving contract to his "
        "brother-in-law's firm without competitive bidding.",
        "corruption",
    ),
    (
        "Researchers announced the city's new light rail line carried two "
        "million riders in its first quarter, beating projections.",
        "transit",
    ),
    (
        "County health inspectors found listeria at three packaged-salad "
        "facilities but the recall was delayed nine days.",
        "public_health",
    ),
    (
        "The school board voted 5-2 to adopt the new math curriculum after "
        "a heated public comment session.",
        "education",
    ),
    (
        "Leaked emails show the police chief ordered officers to stop "
        "logging use-of-force incidents in the public database.",
        "corruption",
    ),
    (
        "A new study finds the regional bus network's on-time rate fell to "
        "61 percent after schedule cuts.",
        "transit",
    ),
]

LABELS = ["corruption", "transit", "public_health", "education", "other"]


@pytest.mark.parametrize("model", ["gemini/gemini-3.5-flash"])
def test_golden_classify_categories(tmp_path, model, golden_model_keys):
    p = Project.create(tmp_path / "golden.frisket", name="golden-classify")
    try:
        sheet = p.add_sheet("stories")
        cols = {"snippet": p.add_column(sheet, "snippet")}
        p.add_rows(sheet, [{"snippet": s} for s, _ in SNIPPETS], cols)

        spec = {
            "action_kind": "map.classify",
            "engine": "llm",
            "model": model,
            "sheet_id": sheet,
            "input_columns": ["snippet"],
            "context": "Each row is a one-sentence local news story summary.",
            "fields": [
                {
                    "name": "beat",
                    "type": "category",
                    "labels": LABELS,
                    "description": "Which news beat does this story belong to?",
                }
            ],
            "include_justification": True,
        }
        router = ModelRouter(
            keys=golden_model_keys,
            use_env_keys=False,
            cache=ResponseCache(CACHE_PATH),
            cache_mode=MODE,
        )
        runner = MapRunner(p, router, authority=UnroutedOnlyAuthority(p))

        est = estimate_model_run(runner, spec)
        assert est["cost"] < 0.05, "golden must stay cheap"

        progress = asyncio.run(run_with_exact_confirmation(runner, spec))
        assert progress.done and progress.failed == 0

        beat_col = next(c for c in p.columns(sheet) if c["name"] == "beat")
        just_col = next(
            c for c in p.columns(sheet) if c["name"] == "beat_justification"
        )
        values = p.get_values(sheet, beat_col["id"])
        justs = p.get_values(sheet, just_col["id"])

        # accuracy against hand labels: require 100% on this easy set
        row_ids = [
            r["id"] for r in p.db.execute("SELECT id FROM rows ORDER BY position")
        ]
        got = [values[r] for r in row_ids]
        want = [label for _, label in SNIPPETS]
        assert got == want, f"misclassified: {list(zip(got, want))}"
        assert all(justs[r] and len(justs[r]) > 10 for r in row_ids)

        # provenance: the run envelope is queryable
        run = p.db.execute(
            "SELECT * FROM runs WHERE id=?", (progress.run_id,)
        ).fetchone()
        assert run["model"] == model
        assert run["prompt_hash"]
        asyncio.run(router.aclose())
    finally:
        p.close()


def test_vision_input_reaches_model(tmp_path, golden_model_keys):
    """Image-column blobs must reach the model as
    pixels. A synthetic card (red background, the word FRISKET) leaves no
    room for interpretation: reading the text proves vision input works."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    from frisket.ai.llm import ModelRouter
    from frisket.engine.runner import MapRunner

    img = np.zeros((220, 640, 3), dtype=np.uint8)
    img[:] = (40, 40, 200)  # BGR red card
    cv2.putText(
        img, "FRISKET", (60, 140), cv2.FONT_HERSHEY_DUPLEX, 3, (255, 255, 255), 8
    )
    path = tmp_path / "card.png"
    cv2.imwrite(str(path), img)

    p = Project.create(tmp_path / "v.frisket")
    try:
        sheet = p.add_sheet("cards")
        cols = {"card": p.add_column(sheet, "card", type="image")}
        digest = p.add_blob(path.read_bytes(), filename="card.png", mime="image/png")
        p.add_rows(sheet, [{"card": {"blob": digest, "mime": "image/png"}}], cols)
        router = ModelRouter(
            keys=golden_model_keys,
            use_env_keys=False,
            cache=ResponseCache(CACHE_PATH),
            cache_mode=MODE,
        )
        runner = MapRunner(p, router, authority=UnroutedOnlyAuthority(p))
        prog = asyncio.run(
            run_with_exact_confirmation(
                runner,
                {
                    "action_kind": "map.extract",
                    "model": "gemini/gemini-3.5-flash",
                    "sheet_id": sheet,
                    "input_columns": ["card"],
                    "instruction": "Read the single word printed on the card and "
                    "name the background color.",
                    "fields": [
                        {"name": "word", "type": "text"},
                        {
                            "name": "background",
                            "type": "category",
                            "labels": ["red", "green", "blue", "white"],
                        },
                    ],
                },
            )
        )
        assert prog.failed == 0, [
            dict(row)
            for row in p.db.execute(
                "SELECT error_code, error FROM results WHERE run_id=?",
                (prog.run_id,),
            )
        ]
        cols2 = {c["name"]: c["id"] for c in p.columns(sheet)}
        (word,) = p.get_values(sheet, cols2["word"]).values()
        (bg,) = p.get_values(sheet, cols2["background"]).values()
        assert word.strip().upper() == "FRISKET"
        assert bg == "red"
        asyncio.run(router.aclose())
    finally:
        p.close()
