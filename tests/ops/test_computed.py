"""Computed (non-AI) columns + media ops: regex, template, python sandbox,
face extraction. All offline."""

import asyncio

import pytest
from pydantic import ValidationError

from action_test_helpers import run_typed_map_request, typed_map_request
from frisket.ai.llm import ModelRouter
from frisket.engine.runner import MapRunner
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.engine.store import Project
from runner_test_helpers import run_with_output_claim


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "p.frisket")
    yield p
    p.close()


def run_recipe(p, spec):
    router = ModelRouter(keys={"anthropic": "k"})
    return asyncio.run(
        run_with_output_claim(
            MapRunner(p, router, authority=UnroutedOnlyAuthority(p)),
            spec,
        )
    )


def seed(p, rows):
    sheet = p.add_sheet("data")
    cols = {k: p.add_column(sheet, k) for k in rows[0]}
    p.add_rows(sheet, rows, cols)
    return sheet


class TestComputedColumns:
    def test_regex_extract(self, project):
        sheet = seed(
            project,
            [
                {"text": "Call me at 212-555-0123 tomorrow"},
                {"text": "no phone here"},
            ],
        )
        result = run_typed_map_request(
            project,
            typed_map_request(
                "map.regex_extract",
                sheet,
                params={
                    "input_columns": ["text"],
                    "pattern": r"\d{3}-\d{3}-\d{4}",
                },
                output_names={"extracted": "phone"},
                idempotency_key="computed-regex@1",
            ),
            project_id="computed",
        )
        assert result.status == "completed"
        col = next(c for c in project.columns(sheet) if c["name"] == "phone")
        vals = sorted(
            project.get_values(sheet, col["id"]).values(), key=lambda v: v or ""
        )
        assert vals == [None, "212-555-0123"] or vals == ["212-555-0123", None]

    def test_regex_all_matches(self, project):
        sheet = seed(project, [{"text": "ids: A12, B34, C56"}])
        result = run_typed_map_request(
            project,
            typed_map_request(
                "map.regex_extract",
                sheet,
                params={
                    "input_columns": ["text"],
                    "pattern": r"[A-Z]\d\d",
                    "all_matches": True,
                },
                output_names={"extracted": "ids"},
                idempotency_key="computed-regex-all@1",
            ),
            project_id="computed",
        )
        assert result.status == "completed"
        col = next(c for c in project.columns(sheet) if c["name"] == "ids")
        (val,) = project.get_values(sheet, col["id"]).values()
        assert val == ["A12", "B34", "C56"]

    def test_bad_regex_is_rejected_before_run(self, project):
        sheet = seed(project, [{"text": "x"}])
        with pytest.raises(ValidationError, match="invalid regex pattern"):
            run_typed_map_request(
                project,
                typed_map_request(
                    "map.regex_extract",
                    sheet,
                    params={
                        "input_columns": ["text"],
                        "pattern": "([unclosed",
                    },
                    output_names={"extracted": "out"},
                    idempotency_key="computed-bad-regex@1",
                ),
                project_id="computed",
            )
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0

    def test_template(self, project):
        sheet = seed(project, [{"first": "Linda", "last": "Reyes"}])
        result = run_typed_map_request(
            project,
            typed_map_request(
                "map.template",
                sheet,
                params={"template": {"text": "{{last}}, {{first}}"}},
                output_names={"rendered": "sorted_name"},
                idempotency_key="computed-template@1",
            ),
            project_id="computed",
        )
        assert result.status == "completed"
        col = next(c for c in project.columns(sheet) if c["name"] == "sorted_name")
        (val,) = project.get_values(sheet, col["id"]).values()
        assert val == "Reyes, Linda"

    def test_python_snippet_sandboxed(self, project):
        sheet = seed(project, [{"amount": "1,234.50"}, {"amount": "99"}])
        result = run_typed_map_request(
            project,
            typed_map_request(
                "map.python",
                sheet,
                params={
                    "input_columns": ["amount"],
                    "code": "result = float(row['amount'].replace(',', ''))",
                    "return_schema": {"type": "number"},
                    "output_routes": [
                        {
                            "name": "amount_num",
                            "path": "$",
                            "target": {"kind": "column", "type": "number"},
                        }
                    ],
                },
                output_names={"amount_num": "amount_num"},
                idempotency_key="computed-python-number",
            ),
            project_id="computed",
        )
        assert result.status == "completed", result.errors
        col = next(c for c in project.columns(sheet) if c["name"] == "amount_num")
        assert sorted(project.get_values(sheet, col["id"]).values()) == [99.0, 1234.5]

    def test_python_cannot_see_keys(self, project, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        sheet = seed(project, [{"x": "1"}])
        result = run_typed_map_request(
            project,
            typed_map_request(
                "map.python",
                sheet,
                params={
                    "input_columns": ["x"],
                    "code": "import os; result = sorted(os.environ)",
                    "return_schema": {"type": "array", "items": {"type": "string"}},
                    "output_routes": [
                        {
                            "name": "env",
                            "path": "$",
                            "target": {"kind": "column", "type": "json"},
                        }
                    ],
                },
                output_names={"env": "env"},
                idempotency_key="computed-python-environment",
            ),
            project_id="computed",
        )
        assert result.status == "completed", result.errors
        col = next(c for c in project.columns(sheet) if c["name"] == "env")
        (val,) = project.get_values(sheet, col["id"]).values()
        assert not any("API_KEY" in k for k in val)


class TestFaces:
    def test_extract_faces_handles_no_face_image(self, project, tmp_path):
        cv2 = pytest.importorskip("cv2")
        import numpy as np

        # A test-corpus portrait is unavailable here, so this covers the
        # no-face path and plumbing; YuNet's real-photo coverage lives in
        # test_extract_faces_yunet.py.
        img = np.full((200, 200, 3), 255, dtype=np.uint8)
        path = tmp_path / "blank.jpg"
        cv2.imwrite(str(path), img)
        sheet = project.add_sheet("photos")
        cols = {"photo": project.add_column(sheet, "photo", type="image")}
        digest = project.add_blob(
            path.read_bytes(), filename="blank.jpg", mime="image/jpeg"
        )
        project.add_rows(
            sheet,
            [
                {
                    "photo": {
                        "blob": digest,
                        "filename": "blank.jpg",
                        "mime": "image/jpeg",
                    }
                }
            ],
            cols,
        )
        prog = run_typed_map_request(
            project,
            typed_map_request(
                "media.extract_faces",
                sheet,
                params={"source": "photo"},
                output_names={"faces": "faces"},
                idempotency_key="computed-face",
            ),
            project_id="computed",
        )
        assert prog.status == "completed", prog.errors
        col = next(c for c in project.columns(sheet) if c["name"] == "faces")
        (val,) = project.get_values(sheet, col["id"]).values()
        assert val == []  # blank image: zero faces, zero errors


class TestNetGuard:
    def test_blocks_private_and_metadata(self, monkeypatch):
        import socket

        from frisket.ops import egress_policy
        from frisket.ops.netguard import url_is_safe

        # public-host resolution is stubbed: no live DNS in the offline suite
        # (this line used to be `in (True, False)` with real DNS — flaky AND
        # assertion-free; now it's deterministic and actually asserts)
        real = socket.getaddrinfo
        monkeypatch.setattr(
            egress_policy.socket,
            "getaddrinfo",
            lambda host, *a, **k: (
                [(socket.AF_INET, None, None, "", ("93.184.216.34", 443))]
                if host == "example.com"
                else real(host, *a, **k)
            ),
        )
        assert url_is_safe("https://example.com") is True
        assert url_is_safe("http://127.0.0.1/") is False
        assert url_is_safe("http://localhost:8000/") is False
        assert url_is_safe("http://169.254.169.254/latest/meta-data/") is False
        assert url_is_safe("http://10.0.0.5/") is False
        assert url_is_safe("http://192.168.1.1/") is False
        assert url_is_safe("file:///etc/passwd") is False
        assert url_is_safe("ftp://example.com") is False

    def test_nat64_dns64_v4_in_v6_not_bypassable(self, monkeypatch):
        """SSRF bypass found 2026-06-12: NAT64/DNS64 resolvers answer a private
        v4 with a synthesized PUBLIC-looking v6 (provider prefix + v4 in the
        low 32 bits). Checking is_private on the v6 alone lets 10.0.0.5 through.
        netguard must unwrap the embedded v4 and reject it."""
        import ipaddress
        import socket

        from frisket.ops import egress_policy
        from frisket.ops.netguard import url_is_safe

        # 2607:7700:0:e:0:2:a00:5  == provider-NAT64 prefix wrapping 10.0.0.5
        synth = "2607:7700:0:e:0:2:a00:5"
        monkeypatch.setattr(
            egress_policy,
            "_discovered_nat64_prefixes",
            lambda: (ipaddress.IPv6Network("2607:7700:0:e:0:2::/96"),),
        )
        monkeypatch.setattr(
            egress_policy.socket,
            "getaddrinfo",
            lambda host, *a, **k: [(socket.AF_INET6, None, None, "", (synth, 0, 0, 0))],
        )
        assert url_is_safe("http://10.0.0.5/") is False, (
            "NAT64-synthesized v6 wrapping a private v4 bypassed the guard"
        )
