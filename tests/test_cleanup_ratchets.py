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
glossary = _load("lint_glossary_avoid")
file_counts = _load("file_count_ratchet")
web_types = _load("web_tests_type_ratchet")
collection = _load("check_test_collection")
web_hygiene = _load("lint_web_source_hygiene")
test_tokens = _load("lint_test_tokens")
uv_lock_guard = _load("check_uv_lock_drift")


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


def test_python_skips_follow_a_helper_that_probes_the_environment():
    tree = ast.parse(
        "import shutil, pytest\n"
        "def _gnu_tool():\n"
        "    '#: Needs the available GNU tool.'\n"
        "    for prefix in ('', '/opt/gnubin'):\n"
        "        if shutil.which('stat', path=prefix):\n"
        "            return prefix\n"
        "    pytest.skip('no tool')\n"
        "def _plain_helper(value):\n"
        "    '#: Docstring says installed and available.'\n"
        "    if value:\n"
        "        return value\n"
        "    pytest.skip('no tool is installed')\n"
        "def test_bare():\n"
        "    shutil.which('x')\n"
        "    pytest.skip('later')\n"
    )
    assert supp.python_skips(tree) == {"environment": 1, "debt": 2}


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


def test_file_count_ratchet_reports_slack_and_update_command(capsys):
    baseline = {"a.py": 5, "b.py": 3, "c.py": 2}
    current = {"a.py": 2, "c.py": 2}
    cleared, below = file_counts.slack(baseline, current)
    assert cleared == 6
    assert below == ["a.py", "b.py"]

    code = file_counts.enforce(
        "demo_ratchet",
        baseline,
        current,
        unit="issue(s)",
        fix="Fix them.",
        update=None,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "a.py: 2 < baseline 5" in out
    assert "b.py: 0 < baseline 3" in out
    assert "6 baseline slot(s) cleared across 2 file(s)" in out
    assert "baseline still freezes 10" in out
    assert "python scripts/ci/demo_ratchet.py --update" in out


def test_file_count_ratchet_reports_clean_frozen_state_when_no_slack(capsys):
    baseline = {"a.py": 2}
    code = file_counts.enforce(
        "demo_ratchet",
        baseline,
        {"a.py": 2},
        unit="issue(s)",
        fix="Fix them.",
        update=None,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.strip() == "demo_ratchet: ok (2 issue(s) frozen in 1 files)."


def test_file_count_ratchet_update_lowers_and_refuses_to_grow(capsys):
    written: list[dict[str, int]] = []
    code_ok = file_counts.enforce(
        "demo_ratchet",
        {"a.py": 3, "b.py": 1},
        {"a.py": 1},
        unit="issue(s)",
        fix="Fix them.",
        update=written.append,
    )
    out_ok = capsys.readouterr().out
    assert code_ok == 0
    assert written == [{"a.py": 1}]
    assert "4 -> 1 issue(s)" in out_ok
    assert "3 baseline slot(s) cleared across 2 file(s)" in out_ok

    written.clear()
    code_fail = file_counts.enforce(
        "demo_ratchet",
        {"a.py": 1},
        {"a.py": 2},
        unit="issue(s)",
        fix="Fix them.",
        update=written.append,
    )
    out_fail = capsys.readouterr().out
    assert code_fail == 1
    assert written == []
    assert "above baseline" in out_fail


def test_uv_lock_guard_blocks_lock_only_and_allows_manifest_or_override(
    monkeypatch, capsys
):
    assert uv_lock_guard.check_staged_paths(["src/frisket/cli.py"]) == []
    assert uv_lock_guard.check_staged_paths(["uv.lock"]) == ["uv.lock"]
    assert uv_lock_guard.check_staged_paths(["uv.lock", "pyproject.toml"]) == []
    assert uv_lock_guard.check_staged_paths(["sidecar/retrieval-runtime/uv.lock"]) == [
        "sidecar/retrieval-runtime/uv.lock"
    ]
    assert (
        uv_lock_guard.check_staged_paths(
            [
                "sidecar/retrieval-runtime/uv.lock",
                "sidecar/retrieval-runtime/pyproject.toml",
            ]
        )
        == []
    )
    assert uv_lock_guard.check_staged_paths(["uv.lock"], allow_lock_only=True) == []

    monkeypatch.setattr(uv_lock_guard, "staged_files", lambda: ["uv.lock"])
    monkeypatch.delenv(uv_lock_guard.ALLOW_ENV, raising=False)
    assert uv_lock_guard.main([]) == 1
    err = capsys.readouterr().err
    assert "uv.lock is staged without pyproject.toml" in err
    assert f"{uv_lock_guard.ALLOW_ENV}=1" in err

    monkeypatch.setenv(uv_lock_guard.ALLOW_ENV, "1")
    assert uv_lock_guard.main([]) == 0
    assert capsys.readouterr().err == ""


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


GLOSSARY_SAMPLE = """
**Replace** — a run writes into a generated column.
_Avoid_: overwrite, AI column; "confirmation" for the click.

**Estimate** — what a run is expected to do.
_Avoid_: quote.

**Sheet** — one grid of data.
_Avoid_: table (except one inside a document), dry run.
"""


def test_glossary_avoid_parses_plain_and_qualified_items():
    found = {
        (a.word, a.preferred, a.plain) for a in glossary.parse_glossary(GLOSSARY_SAMPLE)
    }
    assert found == {
        ("overwrite", "Replace", True),
        ("ai column", "Replace", True),
        ("confirmation", "Replace", False),
        ("quote", "Estimate", True),
        ("table", "Sheet", False),
        ("dry run", "Sheet", True),
    }


def test_glossary_avoid_checks_only_plain_unskipped_or_listed_words():
    avoided = glossary.parse_glossary(GLOSSARY_SAMPLE)
    scopes = {"skip_text": {"quote": "x"}, "qualified_text": {"table": "x"}}
    assert glossary.checked_words(avoided, "text", scopes) == {
        "overwrite": "Replace",
        "ai column": "Replace",
        "table": "Sheet",
        "dry run": "Sheet",
    }


def test_glossary_avoid_counts_whole_words_and_plurals():
    words = {"watch": "Alert", "dry run": "Preview"}
    texts = ["Watches panel", "Watch it", "watchdog", "a dry  run", "Rewatch"]
    assert glossary.count_words(texts, words) == {"watch": 2, "dry run": 1}


def test_glossary_avoid_ignores_bare_tokens_and_substitutions():
    words = {"job": "Run"}
    assert glossary.count_words(["job_id", "${job.id}", "a job"], words) == {"job": 1}


def test_glossary_avoid_counts_name_components():
    words = {"overwrite": "Replace", "saved view": "View"}
    names = [
        "overwriteExisting",
        "overwrites_column_id",
        "savedViewsStore",
        "overwritten",
    ]
    assert glossary.count_names(names, words) == {"overwrite": 2, "saved view": 1}


def test_glossary_avoid_reads_jsx_text_and_labels_not_generics():
    source = (
        "type T = Record<string, number>;\n"
        'const a = <Button title="Replace it">Watch list</Button>;\n'
    )
    assert glossary.web_text(source) == ["Replace it", "Watch list"]


def test_glossary_avoid_reads_user_error_and_help_strings_only():
    source = (
        'raise ActionError("Overwrite it", code="x_y")\n'
        'add(help="Pick a recipe")\n'
        'log("a quote here")\n'
    )
    assert glossary.python_text(source) == ["Overwrite it", "x_y", "Pick a recipe"]


def test_native_select_failures_catches_unescaped_select_in_non_allowlisted_file(
    tmp_path, monkeypatch
):
    web = tmp_path / "web"
    src = web / "src"
    components = src / "components"
    components.mkdir(parents=True)
    (components / "Widget.tsx").write_text(
        'export function Widget() { return <select aria-label="Kind" />; }\n',
        encoding="utf-8",
    )
    (components / "PanelSelect.tsx").write_text(
        'export function PanelSelect() { return <select data-native-select-escape="panel-select-trigger" />; }\n',
        encoding="utf-8",
    )
    (components / "FakeEscape.tsx").write_text(
        'export function FakeEscape() { return <select data-native-select-escape="panel-select-trigger" />; }\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(web_hygiene, "REPO", tmp_path)
    monkeypatch.setattr(web_hygiene, "WEB", web)
    monkeypatch.setattr(web_hygiene, "SRC", src)

    failures = web_hygiene.native_select_failures()
    assert len(failures) == 2
    assert failures[0].startswith("web/src/components/FakeEscape.tsx:1:")
    assert "unrecognized escape 'panel-select-trigger'" in failures[0]
    assert failures[1].startswith("web/src/components/Widget.tsx:1:")
    assert "native <select> is forbidden" in failures[1]


def test_native_select_escapes_are_present_and_live():
    assert web_hygiene.NATIVE_SELECT_ESCAPES
    for rel, reason in web_hygiene.NATIVE_SELECT_ESCAPES.items():
        path = web_hygiene.WEB / rel
        assert path.is_file(), f"Stale NATIVE_SELECT_ESCAPES entry: {rel}"
        assert f'data-native-select-escape="{reason}"' in path.read_text(
            encoding="utf-8"
        )


def _token_rule(name: str):
    return next(rule for rule in test_tokens.RULES if rule.name == name)


def test_source_reads_ast_catches_multiline_chain_and_scopes_taint():
    rule = _token_rule("source-reads")
    source = (
        "from pathlib import Path\n"
        "def test_multiline():\n"
        "    text = (\n"
        '        Path(__file__).resolve().parents[2] / "sidecar/workers/dots/Dockerfile"\n'
        "    ).read_text()\n"
        "    assert text\n"
        "def test_marked():\n"
        "    # rule19: intentional artifact verification\n"
        "    text = (\n"
        '        Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"\n'
        "    ).read_text()\n"
        "    assert text\n"
        "def test_sibling_taint_isolated(tmp_path):\n"
        '    root = tmp_path / "data.txt"\n'
        "    assert root.read_text() == ''\n"
        "def test_other_taints_root():\n"
        '    root = Path(__file__).resolve().parents[2] / "src"\n'
        "    assert root.exists()\n"
    )
    offenders = test_tokens.offending_lines(rule, source)
    assert [lineno for lineno, _ in offenders] == [3]


def test_pure_surface_assertions_flags_reflection_only_and_honors_invariant():
    rule = _token_rule("pure-surface-assertions")
    source = (
        "def test_only_hasattr():\n"
        "    import frisket\n"
        '    assert hasattr(frisket, "x")\n'
        "# invariant: boundary surface contract\n"
        "def test_marked_invariant():\n"
        "    import frisket\n"
        '    assert not hasattr(frisket, "removed")\n'
        "def test_behavioral():\n"
        "    result = compute(1)\n"
        "    assert result is not None\n"
    )
    offenders = test_tokens.offending_lines(rule, source)
    assert [lineno for lineno, _ in offenders] == [1]


def test_conditional_only_assertions_flags_dynamic_loops_but_allows_preconditions():
    rule = _token_rule("conditional-only-assertions")
    source = (
        "CONST = ('a', 'b')\n"
        "def test_dynamic_loop_only():\n"
        "    for item in get_items():\n"
        "        assert item.ok\n"
        "def test_guarded_loop():\n"
        "    items = get_items()\n"
        "    assert items\n"
        "    for item in items:\n"
        "        assert item.ok\n"
        "def test_constant_tuple_loop():\n"
        "    for item in CONST:\n"
        "        assert check(item)\n"
        "def test_if_without_else():\n"
        "    if maybe():\n"
        "        assert True\n"
        "def test_if_else_both_assert():\n"
        "    if maybe():\n"
        "        assert check(1)\n"
        "    else:\n"
        "        assert check(2)\n"
    )
    offenders = test_tokens.offending_lines(rule, source)
    assert [lineno for lineno, _ in offenders] == [2, 13]
