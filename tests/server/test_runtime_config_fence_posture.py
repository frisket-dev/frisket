"""`GET /api/config` tells the browser what confines a `map.python` recipe.

The editor's trust line was wrong in both directions inside one day: a green
"sandboxed" badge for a recipe `ctypes` walked out of, then an unconditional
"not sandboxed" warning that survived the seccomp+Landlock fence landing and
made Linux -- the platform Frisket deploys on -- look unprotected. The browser
cannot know the server's platform, so the server says it, once, from the
fence's own answer.

What this file pins: the wire value is the mint's value (no second opinion in
the route), the mint tracks the fence's own platform matrix rather than
restating it, and an unheard-of platform reports `unknown` rather than
guessing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.engine import recipe_fence_posture as posture_mod
from frisket.engine.recipe_fence_posture import recipe_fence_posture
from frisket.engine.sandbox import fence
from frisket.server.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestRuntimeConfigReportsTheFencePosture:
    def test_the_route_relays_the_mint_and_does_not_recompute_it(self, tmp_path):
        client = TestClient(create_app(tmp_path / "workspace"))
        body = client.get("/api/config").json()
        assert body["recipe_fence_posture"] == recipe_fence_posture()

    @pytest.mark.skipif(
        sys.platform != "linux", reason="only Linux has the kernel fence to report"
    )
    def test_linux_reports_enforced(self, tmp_path):
        client = TestClient(create_app(tmp_path / "workspace"))
        assert client.get("/api/config").json()["recipe_fence_posture"] == "enforced"

    @pytest.mark.parametrize(
        ("platform", "expected"),
        [("darwin", "partial"), ("win32", "none"), ("haiku1", "unknown")],
    )
    def test_an_unfenced_server_says_so_on_the_wire(
        self, tmp_path, monkeypatch, platform, expected
    ):
        """Red-proof for the copy: make the server report an unenforced posture.

        `sys.platform` is the one input -- the fence reads the same module
        object -- so this is the whole route, not a stubbed posture function.
        """
        monkeypatch.setattr(sys, "platform", platform)
        client = TestClient(create_app(tmp_path / "workspace"))
        assert client.get("/api/config").json()["recipe_fence_posture"] == expected


class TestThePostureIsDerivedFromTheFence:
    def test_enforced_means_exactly_what_the_fence_calls_enforced(self, monkeypatch):
        """Not a platform list of its own: enforced == a kernel policy exists."""
        assert (recipe_fence_posture() == "enforced") == bool(
            fence.bootstrap_policy(audit_netwall=True, recipe=True)["fence"]
        )
        for platform in fence._UNENFORCED_PLATFORM_NOTES:
            monkeypatch.setattr(sys, "platform", platform)
            assert recipe_fence_posture() != "enforced"

    def test_the_unenforced_platforms_are_the_fences_own_set(self):
        """Closure test: the fence adding or renaming a platform turns this red.

        Without it, a new unenforced platform would quietly report `unknown`
        (which at least warns) or, worse, a stale entry here would keep
        claiming a wall the fence no longer describes.
        """
        assert set(posture_mod._UNENFORCED_POSTURES) == set(
            fence._UNENFORCED_PLATFORM_NOTES
        )
        assert "enforced" not in posture_mod._UNENFORCED_POSTURES.values()

    def test_an_unnamed_platform_fails_closed(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "freebsd14")
        assert recipe_fence_posture() == "unknown"


class TestTheBrowserSpeaksTheSameVocabulary:
    def test_the_typescript_union_matches_the_python_literal(self):
        """Mirror across the language boundary (CLAUDE.md: two surfaces).

        A posture the server can send but the client has no arm for degrades to
        `unknown` at runtime (`asRecipeFencePosture`), so this is a copy bug,
        not a crash -- but it is still a copy bug, and it is cheap to catch.
        """
        # rule19: diffs the Python posture vocabulary vs the independently maintained TS union
        types_ts = (REPO_ROOT / "web/src/api/types.ts").read_text(encoding="utf-8")
        match = re.search(
            r"export type RecipeFencePosture\s*=\s*([^;]+);", types_ts, re.S
        )
        assert match, "web/src/api/types.ts no longer declares RecipeFencePosture"
        declared = set(re.findall(r"'([a-z]+)'", match.group(1)))
        assert declared == {"enforced", "partial", "none", "unknown"}
