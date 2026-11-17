"""Pure-logic tests for the shrink-only cleanup ratchets in scripts/ci."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

CI = Path(__file__).parents[1] / "scripts/ci"
# Gate scripts import their shared helpers as siblings, as they do when run.
sys.path.insert(0, str(CI))


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, CI / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ruff_ratchet = _load("ruff_ratchet")
dead = _load("check_dead_symbols")
dup = _load("check_duplicate_functions")
route_idioms = _load("lint_route_error_idioms")

scan = _load("source_scan")
supp = _load("check_suppressions")
history = _load("lint_comment_history")
file_counts = _load("file_count_ratchet")
web_types = _load("web_tests_type_ratchet")
collection = _load("check_test_collection")


def test_ruff_ratchet_flags_increases_and_new_files_only():
    baseline = {"C901": {"a.py": 2}, "PLC0415": {"a.py": 1}}
    current = {"C901": {"a.py": 1, "b.py": 1}, "PLC0415": {"a.py": 2}}
    increases, decreases = ruff_ratchet.compare(baseline, current)
    assert increases == [
        "b.py: C901 1 > baseline 0",
        "a.py: PLC0415 2 > baseline 1",
    ]
    assert decreases == ["a.py: C901 1 < baseline 2"]


def test_dead_symbol_references_ignore_reexports_all_and_self_use():
    tree = ast.parse(
        "from .m import reexported, aliased as other\n"
        "__all__ = ['listed']\n"
        "def recursive():\n    return recursive()\n"
        "getattr(obj, 'by_string')\n"
        "obj.attribute\n"
    )
    refs = dead.References()
    refs.scan(tree, "frisket.pkg.mod", is_package=False)
    assert {"aliased", "by_string", "attribute"} <= set(refs.names)
    assert not {"reexported", "listed", "recursive"} & set(refs.names)
    assert {"frisket.pkg.m", "frisket.pkg.m.reexported"} <= refs.modules


def test_duplicate_shape_erases_names_and_strings_but_not_structure():
    def body(source: str) -> str:
        return dup.shape(ast.parse(source).body[0].body)

    assert body("def f(a):\n    return a + 'x'") == body(
        "def g(b):\n    return b + 'y'"
    )
    assert body("def f(a):\n    return a + 1") != body("def f(a):\n    return a - 1")


def test_duplicate_groups_may_shrink_but_not_grow_or_appear():
    baseline = {"near": [["a::f", "b::f", "c::f"]], "private": []}
    shrunk = {"near": [["a::f", "b::f"]], "private": []}
    grown = {"near": [["a::f", "b::f", "d::f"]], "private": []}
    fresh = {"near": [], "private": [["x::_h", "y::_h", "z::_h"]]}
    assert dup.regressions(baseline, shrunk) == []
    assert "not in baseline: d::f" in dup.regressions(baseline, grown)[0]
    assert "not in baseline: x::_h, y::_h, z::_h" in dup.regressions(baseline, fresh)[0]


def test_class_body_binding_defines_a_method_and_is_not_a_use():
    tree = ast.parse(
        "class Facade:\n    read = leaf.read\n    renamed = leaf.other\n    limit = 3\n"
    )
    refs = dead.References()
    refs.scan(tree, "pkg.facade", False)
    assert refs.names["read"] == 0
    assert refs.names["other"] == 1
    assert refs.names["renamed"] == 0
    found = dict(dead.candidates(tree, "facade.py"))
    assert found["facade.py::Facade.read"] == "read"
    assert found["facade.py::Facade.renamed"] == "renamed"
    assert "facade.py::Facade.limit" not in found


def test_route_error_idioms_count_per_function_and_only_shrink(tmp_path):
    routes = tmp_path / "routes"
    routes.mkdir()
    (routes / "r.py").write_text(
        "def register(app):\n"
        "    def handler():\n"
        "        try:\n"
        "            work()\n"
        "        except ValueError as exc:\n"
        "            raise HTTPException(400, str(exc)) from exc\n"
        "        raise HTTPException(404, 'outside an except block')\n"
        "    def forked():\n"
        "        return JSONResponse(status_code=409, content={})\n"
        "    def ok():\n"
        "        return JSONResponse(content={})\n"
    )
    route_idioms.REPO = tmp_path
    found = route_idioms.offenders(routes)
    assert found == {
        "routes/r.py::register.handler: except -> raise HTTPException": 1,
        "routes/r.py::register.forked: return JSONResponse(status_code=)": 1,
    }
    assert route_idioms.compare(found, found) == []
    grown = route_idioms.compare(found, {**found, next(iter(found)): 0})
    assert len(grown) == 1 and "allowlist: 0" in grown[0]
    stale = route_idioms.compare({}, found)
    assert len(stale) == 2 and all("no longer offends" in line for line in stale)


def test_js_segments_separate_comments_literals_and_code():
    source = (
        "const a = 'it''s // not a comment';\n"
        "const re = /\\/\\/[/]x/g; // real comment\n"
        "const t = `x ${y /* inner */ + 1} // still template`;\n"
        "const half = n / 2 / 3;\n"
        "<p>Don't</p>;\n"
        "</div>; // tail\n"
    )
    segments = list(scan.js_segments(source))
    assert "".join(text for _, _, text in segments) == source
    comments = [(line, text) for kind, line, text in segments if kind == "comment"]
    assert comments == [
        (2, "// real comment"),
        (3, "/* inner */"),
        (6, "// tail"),
    ]
    code = dict(scan.js_code_lines(source))
    assert "n / 2 / 3" in code[4]


def test_python_comments_and_docstrings_ignore_string_literals():
    source = 'x = "# noqa: E501"  # noqa: F401\ndef f():\n    """Doc."""\n'
    assert scan.python_comments(source) == [(1, "# noqa: F401")]
    assert scan.python_docstrings(ast.parse(source)) == [(3, "Doc.")]


def test_suppressions_count_comment_kinds_not_strings():
    tally = supp.measure_python(
        's = "# type: ignore"  # type: ignore[arg-type]  # noqa: E501\n'
        "y = 1  # noqa\n"
        "z = 2  # pragma: no cover\n"
    )
    assert tally["py-type-ignore"] == 1
    assert tally["py-noqa"] == 1
    assert tally["py-noqa-bare"] == 1
    assert tally["py-pragma-no-cover"] == 1


def test_python_skips_split_environment_probes_from_debt():
    tree = ast.parse(
        "import shutil, sys, pytest\n"
        "try:\n    import fitz\nexcept ImportError:\n    fitz = None\n"
        "FFMPEG = shutil.which('ffmpeg')\n"
        "@pytest.mark.skipif(FFMPEG is None, reason='no ffmpeg')\n"
        "def test_a(): ...\n"
        "@pytest.mark.skipif(fitz is None, reason='no fitz')\n"
        "def test_b(): ...\n"
        "@pytest.mark.skipif(sys.platform == 'win32', reason='posix')\n"
        "def test_c(): ...\n"
        "@pytest.mark.skip(reason='flaky')\n"
        "def test_d(): ...\n"
        "@pytest.mark.skipif(True, reason='broken')\n"
        "def test_e(): ...\n"
        "@pytest.mark.xfail\n"
        "def test_f(): ...\n"
        "def test_g():\n    pytest.skip('later')\n"
        "def test_h():\n    if result.count == 3:\n        pytest.skip('odd')\n"
        "def test_i():\n    pytest.importorskip('pyarrow')\n"
    )
    skips = supp.python_skips(tree)
    assert skips == {"environment": 4, "debt": 5}


def test_js_skip_debt_allows_environment_gated_skips():
    lines = list(
        enumerate(
            [
                "const d = process.env.CI ? describe : describe.skip;",
                "",
                "",
                "",
                "",
                "it.skip('later', () => {});",
            ],
            start=1,
        )
    )
    assert supp.js_skip_debt(lines) == 1


def test_suppressions_only_is_banned_even_when_baselined():
    baseline = {"js-test-only": {"a.test.ts": 1}, "ts-as-never": {"a.ts": 2}}
    current = {"js-test-only": {"a.test.ts": 1}, "ts-as-never": {"a.ts": 1}}
    increases, decreases = supp.compare(baseline, current)
    assert increases == ["a.test.ts: js-test-only 1 > baseline 0"]
    assert decreases == ["a.ts: ts-as-never 1 < baseline 2"]


def test_comment_history_flags_history_but_not_standards_or_plain_words():
    flagged = [
        "# captured 2026-07-02 before the fix",
        "// WEB-01 requirement 5",
        "# Codex review hardening",
        "# per the review's bar",
        "# TODO: tighten",
        "// for now keep both",
        "# the legacy reducer still owns it",
    ]
    clean = [
        "# UTF-16 offsets and SHA-256 digests",
        "# ISO-8601 timestamps, CC-BY-4.0 licence",
        "# the clip landed in the blob store",
        "# a temporary file per request",
        "# legacy rows predate the column",
        "# Claude models reject forced tools",
    ]
    hits = history.comment_hits([(i, text) for i, text in enumerate(flagged + clean)])
    assert sorted({line for line, _, _ in hits}) == list(range(len(flagged)))


def test_file_counts_may_shrink_but_not_grow_or_appear():
    above, below = file_counts.compare({"a.py": 2, "b.py": 1}, {"a.py": 1, "c.py": 1})
    assert above == ["c.py"]
    assert below == ["a.py", "b.py"]


def test_web_test_type_errors_parse_the_first_line_of_each_error():
    first = "tests/unit/a.test.ts(3,7): error TS2322: Type 'x' is not assignable to type 'y'."
    match = web_types.ERROR.match(first)
    assert (
        match and match["file"] == "tests/unit/a.test.ts" and match["code"] == "TS2322"
    )
    assert web_types.ERROR.match("  Type 'x' is not assignable to type 'y'.") is None


def test_collection_parses_collected_and_import_skipped_files():
    assert (
        collection.COLLECTED_LINE.match("tests/a/test_x.py: 3")["file"]
        == "tests/a/test_x.py"
    )
    skipped = "SKIPPED [1] tests/b/test_y.py:12: could not import 'pyarrow'"
    assert collection.SKIPPED_LINE.match(skipped)["file"] == "tests/b/test_y.py"
    assert collection.COLLECTED_LINE.match("11 tests collected in 0.4s") is None
