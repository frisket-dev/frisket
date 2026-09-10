"""The FRISKET_ALLOW_CODE_RECIPES deny guards, derived from declarations.

Two guards refuse actions when ``FRISKET_ALLOW_CODE_RECIPES=0``:

- ``mcp/backends.py`` LocalBackend.run_action
- ``executor/action_families/runs.py`` _guard_backfill_run_spec

Both share ONE predicate, ``gated_capability_phrase``. It accepts only the
canonical action kind and answers from the action's DECLARED
``required_capabilities`` in the contract registry — there is no list of
action kinds to keep in sync. The env gate stays at each call site, so a
trusted/local server (``=1``) is unaffected.

``TestGatedCapabilityClosure`` is the point of the derivation: it pins WHICH
capabilities the flag gates, forces every ``unsafe:*`` capability declared
anywhere to be gated, and makes every ``external:*`` capability state its
disposition. A new capability in either family turns it red, so the question
gets decided rather than silently defaulted.
"""

from __future__ import annotations

import asyncio
from types import MappingProxyType

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    map_rows,
)
from frisket.actions.python_types import PythonEvaluator
from frisket.actions.research_types import Researcher, WebSearcher
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    ModelRef,
    Row,
    RowResult,
)
from http_test_helpers import queued_python_run_spec, v1_action_from_canonical_run_spec


def register_typed_action(monkeypatch, namespace: str, declared) -> str:
    """Register a REAL typed action in the live registry for one test.

    The predicate must read the same registry production reads, so this
    patches ``ACTION_REGISTRY`` and ``NEW_ACTION_IDS`` rather than stubbing
    the lookup. Returns the registered action id.
    """
    import frisket.actions.registry as registry_module

    registered = ActionRegistry((ActionNamespace(namespace, actions=(declared,)),)).get(
        f"{namespace}.{declared.name}"
    )
    monkeypatch.setattr(
        registry_module.ACTION_REGISTRY,
        "_actions",
        MappingProxyType(
            {
                **registry_module.ACTION_REGISTRY._actions,
                registered.action_id: registered,
            }
        ),
    )
    monkeypatch.setattr(
        registry_module,
        "NEW_ACTION_IDS",
        registry_module.NEW_ACTION_IDS | {registered.action_id},
    )
    return registered.action_id


CODE_IDS = ["map.mcp_extract", "map.python", "research.answer"]
# benign kinds a code-disabled server must still allow. research.web_search is
# the load-bearing one: it declares external:web_search exactly like
# research.answer does, and it must stay PERMITTED — which is why the gating
# rule for research.answer is the model:complete + external:web_search PAIR
# and not bare external:web_search.
BENIGN_IDS = [
    "map.extract",
    "map.template",
    "map.classify",
    "map.summarize",
    "research.web_search",
    "enrich.geocode",
    "map.api_call",
    "source.poll",
]


class TestGatedCapabilityPredicate:
    def test_retired_python_id_is_not_gated_but_cannot_execute(self):
        # the retired pre-op-sdk "python" id folds to nothing and declares
        # nothing; the safety story is resolution, not classification: no
        # registry entry carries it, so a guard that passes it can still
        # never run code.
        from frisket.authoring.action_metadata import gated_capability_phrase
        from frisket.ops.builtin import get_recipe

        assert gated_capability_phrase("python") is None
        with pytest.raises(ValueError, match="not canonical"):
            get_recipe("python")

    def test_every_canonical_code_id_is_denied(self):
        from frisket.authoring.action_metadata import (
            canonical_action_kind,
            gated_capability_phrase,
        )

        for cid in CODE_IDS:
            assert gated_capability_phrase(cid) is not None, cid
            assert canonical_action_kind(cid) == cid

    def test_benign_kinds_are_not_gated(self):
        from frisket.authoring.action_metadata import gated_capability_phrase

        for bid in BENIGN_IDS:
            assert gated_capability_phrase(bid) is None, bid

    def test_predicate_is_env_independent(self):
        # the FRISKET_ALLOW_CODE_RECIPES env gate lives at the call sites; the
        # predicate itself only classifies the action.
        from frisket.authoring.action_metadata import gated_capability_phrase

        assert gated_capability_phrase("") is None
        assert gated_capability_phrase(None) is None

    def test_answer_comes_from_declared_capabilities(self):
        from frisket.authoring.action_metadata import declared_action_capabilities

        assert "unsafe:local_code" in declared_action_capabilities("map.python")
        assert declared_action_capabilities("agent") == frozenset()
        # an id with no registry entry declares nothing
        assert declared_action_capabilities("python") == frozenset()


class SyntheticParams(ActionParams):
    source: ColumnRef[str]


class SyntheticAgentParams(ActionParams):
    source: ColumnRef[str]
    model: ModelRef


class SyntheticOutput(BaseModel):
    value: str


async def synthetic_local_code(
    params: SyntheticParams, row: Row, evaluator: PythonEvaluator
) -> RowResult[SyntheticOutput]:
    # declares unsafe:local_code purely by asking for the evaluator.
    value = await evaluator.evaluate(
        code="result = row['value']", row={"value": params.source.read(row)}
    )
    return RowResult(output=SyntheticOutput(value=value))


async def synthetic_model_directed_web(
    params: SyntheticAgentParams, row: Row, researcher: Researcher
) -> RowResult[SyntheticOutput]:
    # declares model:complete + external:web_search purely by asking for the
    # researcher: the MODEL chooses what to fetch.
    result = await researcher.answer(
        row, goal="Describe the value.", context={"value": params.source.read(row)}
    )
    return RowResult(output=SyntheticOutput(value=result.answer))


async def synthetic_search(
    params: SyntheticParams, row: Row, searcher: WebSearcher
) -> RowResult[SyntheticOutput]:
    return RowResult(output=SyntheticOutput(value=params.source.read(row)))


class TestNewActionIsGatedWithoutBeingListed:
    """The whole point: a NEW action declaring a gated capability is refused
    without anyone editing a list of kinds.

    A typed action declares its capabilities by what its handler asks the
    SDK to inject (``PythonEvaluator`` -> unsafe:local_code, ``Researcher``
    -> model:complete + external:web_search, ``WebSearcher`` -> external:web_search);
    ``catalog_entry()`` derives ``required_capabilities``
    from exactly that, and the predicate reads it from the live registry.

    Red-proof: delete the ``declared_action_capabilities`` lookup (make it
    return ``frozenset()``) and this test fails, because nothing else in the
    codebase knows this synthetic kind exists.
    """

    @staticmethod
    def _register_synthetic(monkeypatch, namespace: str, name: str, run) -> str:
        return register_typed_action(
            monkeypatch,
            namespace,
            action(
                name=name,
                title=f"Synthetic {name}",
                description="Synthetic action registered by one test.",
                category=ActionCategory.CONVERT,
                run=run,
            ),
        )

    def test_unlisted_action_declaring_unsafe_local_code_is_gated(self, monkeypatch):
        from frisket.authoring.action_metadata import (
            declared_action_capabilities,
            gated_capability_phrase,
        )

        kind = "map.synthetic_unsafe"
        assert gated_capability_phrase(kind) is None  # unknown before registration
        registered = self._register_synthetic(
            monkeypatch, "map", "synthetic_unsafe", map_rows(synthetic_local_code)
        )
        assert registered == kind
        assert "unsafe:local_code" in declared_action_capabilities(kind)
        phrase = gated_capability_phrase(kind)
        assert phrase is not None, (
            "an action declaring unsafe:local_code must be gated by its "
            "DECLARATION, with no kind added to any list"
        )
        assert "unsafe:local_code" in phrase

    def test_unlisted_action_declaring_model_directed_web_is_gated(self, monkeypatch):
        from frisket.authoring.action_metadata import (
            declared_action_capabilities,
            gated_capability_phrase,
        )

        kind = self._register_synthetic(
            monkeypatch,
            "research",
            "synthetic_agent",
            map_rows(synthetic_model_directed_web),
        )
        assert kind == "research.synthetic_agent"
        assert {"model:complete", "external:web_search"} <= (
            declared_action_capabilities(kind)
        )
        assert gated_capability_phrase(kind) is not None

    def test_unlisted_benign_action_is_not_gated(self, monkeypatch):
        # the derivation must not gate by accident: web search WITHOUT model
        # completion stays permitted, exactly like research.web_search.
        from frisket.authoring.action_metadata import (
            declared_action_capabilities,
            gated_capability_phrase,
        )

        kind = self._register_synthetic(
            monkeypatch,
            "research",
            "synthetic_search",
            map_rows(synthetic_search),
        )
        assert kind == "research.synthetic_search"
        assert declared_action_capabilities(kind) == {
            "project:write",
            "external:web_search",
        }
        assert gated_capability_phrase(kind) is None

    def test_guards_refuse_the_unlisted_action_end_to_end(self, monkeypatch):
        # not just the predicate — the backfill guard itself refuses a kind
        # that appears in no list anywhere.
        from frisket.engine.executor.action_families.runs import (
            _guard_backfill_run_spec,
        )

        monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
        kind = self._register_synthetic(
            monkeypatch, "map", "synthetic_unsafe", map_rows(synthetic_local_code)
        )
        err = _guard_backfill_run_spec({"action_kind": kind})
        assert err is not None
        assert err.code == "code_action_disabled"


class TestGatedCapabilityClosure:
    """Pins WHAT the flag gates, and forces a decision on every new capability.

    Red-proof: add a capability to any action's ``required_capabilities`` in
    the ``unsafe:*`` or ``external:*`` family and one of these fails.
    """

    # Exactly what FRISKET_ALLOW_CODE_RECIPES=0 refuses. Widening this is a
    # product change: it makes actions that work today start failing.
    EXPECTED_GATED_RULES = {
        frozenset({"unsafe:local_code"}),
        frozenset({"model:complete", "external:web_search"}),
    }
    # The kinds those rules refuse today. Unchanged from the hand-written
    # CODE_ACTION_KINDS set this derivation replaced.
    EXPECTED_GATED_KINDS = {"map.mcp_extract", "map.python", "research.answer"}

    # Every external:* capability declared anywhere, and why it is NOT gated
    # on its own. A new external:* capability must be added here with a
    # reason — that is the forced decision.
    EXTERNAL_CAPABILITY_DISPOSITION = {
        "external:api_call": (
            "NOT gated: the caller supplies the URL and body; the per-project "
            "network gate governs it, not the code-action flag."
        ),
        "external:browser_render": (
            "NOT gated: renders a caller-named page in the sandboxed browser; "
            "no code executes and the model directs nothing."
        ),
        "external:geocode": (
            "NOT gated: a fixed-shape lookup against one configured geocoder."
        ),
        "external:google_sheets": (
            "NOT gated: export to a sheet the operator connected; egress of "
            "data the caller already holds."
        ),
        "external:http_fetch": (
            "NOT gated on its own, and model:complete + external:http_fetch "
            "WITHOUT external:web_search (a model-directed fetch) would be "
            "ungated today. research.answer is refused only because every "
            "Researcher action also derives external:web_search "
            "(src/frisket/actions/core.py), which the model:complete pair "
            "gates. A future capability deriving http_fetch alone needs its "
            "own rule; test_http_fetch_never_declared_without_web_search pins "
            "that reliance."
        ),
        "external:media_download": (
            "NOT gated: fetches caller-named media URLs; no code executes and "
            "the model directs nothing."
        ),
        "external:source_poll": (
            "NOT gated: polls a source the operator configured, on a fixed "
            "cursor-advancing shape."
        ),
        "external:url_capture": (
            "NOT gated: captures a caller-named page; no code executes and the "
            "model directs nothing."
        ),
        "external:us_census_acs": (
            "NOT gated: a fixed-shape lookup against one public dataset."
        ),
        "external:web_search": (
            "GATED ONLY IN PAIR with model:complete. research.web_search runs a "
            "caller-supplied query and stays permitted; research.answer lets "
            "the MODEL choose what to fetch, which is what the flag refuses."
        ),
    }

    @staticmethod
    def _declared_capabilities() -> set[str]:
        from frisket.actions.system import root_action_catalog

        declared: set[str] = set()
        for entry in root_action_catalog().actions:
            declared.update(entry.required_capabilities or ())
        return declared

    def test_gated_rules_are_exactly_the_intended_ones(self):
        from frisket.authoring.action_metadata import (
            CODE_RECIPE_GATED_CAPABILITY_RULES,
        )

        actual = {rule.capabilities for rule in CODE_RECIPE_GATED_CAPABILITY_RULES}
        assert actual == self.EXPECTED_GATED_RULES, (
            "the set of capability requirements FRISKET_ALLOW_CODE_RECIPES=0 "
            "refuses changed. Widening it makes actions that work today start "
            "failing; narrowing it un-gates something. Decide deliberately."
        )

    def test_gated_kinds_are_unchanged_from_the_hand_written_set(self):
        # the behavioural contract the derivation had to preserve exactly.
        from frisket.authoring.action_metadata import gated_capability_phrase
        from frisket.actions.system import root_action_catalog

        gated = {
            entry.kind
            for entry in root_action_catalog().actions
            if gated_capability_phrase(entry.kind) is not None
        }
        assert gated == self.EXPECTED_GATED_KINDS

    def test_every_declared_unsafe_capability_is_gated(self):
        # a future unsafe:* capability cannot appear UNGATED by default.
        from frisket.authoring.action_metadata import (
            CODE_RECIPE_GATED_CAPABILITY_RULES,
        )

        unsafe = {c for c in self._declared_capabilities() if c.startswith("unsafe:")}
        assert unsafe, "expected at least unsafe:local_code to be declared somewhere"
        for capability in sorted(unsafe):
            covered = any(
                rule.capabilities == frozenset({capability})
                for rule in CODE_RECIPE_GATED_CAPABILITY_RULES
            )
            assert covered, (
                f"{capability} is declared by an action but is NOT gated by "
                "FRISKET_ALLOW_CODE_RECIPES. An unsafe capability must be "
                "gated on its own, or explicitly argued otherwise here."
            )

    def test_every_declared_external_capability_states_its_disposition(self):
        external = {
            c for c in self._declared_capabilities() if c.startswith("external:")
        }
        documented = set(self.EXTERNAL_CAPABILITY_DISPOSITION)
        assert external == documented, (
            "an external:* capability appeared or disappeared. Each one must "
            "state, in one line, whether the code-action flag gates it and "
            f"why. missing={sorted(external - documented)} "
            f"stale={sorted(documented - external)}"
        )

    def test_http_fetch_never_declared_without_web_search(self):
        # The external:http_fetch disposition relies on this: no gating rule
        # names http_fetch, so a model-directed fetch is refused ONLY via the
        # model:complete + external:web_search pair. That holds today because
        # every action deriving http_fetch derives web_search alongside it.
        # An action that breaks the pairing must bring its own gating rule.
        from frisket.actions.registry import ACTION_REGISTRY
        from frisket.authoring.action_metadata import declared_action_capabilities

        fetchers = {
            kind
            for kind in ACTION_REGISTRY.action_ids
            if "external:http_fetch" in declared_action_capabilities(kind)
        }
        assert fetchers, "expected research.answer to declare external:http_fetch"
        unpaired = {
            kind
            for kind in fetchers
            if "external:web_search" not in declared_action_capabilities(kind)
        }
        assert not unpaired, (
            f"{sorted(unpaired)} declare external:http_fetch without "
            "external:web_search, so the model:complete + external:web_search "
            "rule cannot gate a model-directed fetch there. Add a rule for "
            "http_fetch (or the pair) before shipping such an action."
        )

    def test_ungated_external_capabilities_really_are_permitted(self):
        # the disposition table is not just prose: every capability it calls
        # NOT gated must actually leave its declaring actions permitted.
        from frisket.authoring.action_metadata import gated_capability_phrase
        from frisket.actions.system import root_action_catalog

        ungated = {
            capability
            for capability, reason in self.EXTERNAL_CAPABILITY_DISPOSITION.items()
            if reason.startswith("NOT gated")
        }
        for entry in root_action_catalog().actions:
            kind = entry.kind
            declared = set(entry.required_capabilities or ())
            if declared & ungated and not (declared & {"unsafe:local_code"}):
                if kind in self.EXPECTED_GATED_KINDS:
                    continue
                assert gated_capability_phrase(kind) is None, (
                    f"{kind} declares only capabilities documented as NOT "
                    "gated, but the guards refuse it"
                )


class TestMcpBackendCodeActionGuard:
    def test_backend_rejects_canonical_map_python_when_code_disabled(
        self, tmp_path, monkeypatch
    ):
        # The typed MCP request must still reach the shared service guard.
        monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
        from frisket.server.mcp import LocalBackend

        backend = LocalBackend(tmp_path / "ws")
        spec = v1_action_from_canonical_run_spec(
            queued_python_run_spec(1, "source", "copied")
        )
        with pytest.raises(ValueError) as exc:
            asyncio.run(backend.run_action("demo", spec))
        msg = str(exc.value)
        assert "disabled" in msg
        # the deny message speaks "action", never the internal "recipe" term.
        assert "recipe" not in msg.lower()
        # it names the REFUSED CAPABILITY rather than claiming "runs code".
        assert "unsafe:local_code" in msg
        assert "trusted/local" in msg

    def test_backend_rejects_typed_research_answer_when_code_disabled(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
        from frisket.server.mcp import LocalBackend

        backend = LocalBackend(tmp_path / "ws")
        spec = {
            "action_id": "research.answer",
            "scope": {"kind": "sheet_rows", "sheet_id": 1},
            "params": {
                "source": ["company"],
                "question": {"text": "What does this row establish?"},
                "model": "anthropic/claude-haiku-4-5",
            },
            "idempotency_key": "code-disabled-research-answer",
        }
        with pytest.raises(ValueError) as exc:
            asyncio.run(backend.run_action("demo", spec))
        msg = str(exc.value)
        assert "disabled" in msg
        # research.answer executes NO user code — the old message said it did.
        assert "runs code" not in msg
        assert "external:web_search" in msg

    def test_backend_permits_gated_actions_when_code_enabled(
        self, tmp_path, monkeypatch
    ):
        # red-proof the fence: with the flag ON the guard must not fire, so a
        # failure here is something OTHER than the capability refusal.
        monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "1")
        from frisket.server.mcp import LocalBackend

        backend = LocalBackend(tmp_path / "ws")
        spec = v1_action_from_canonical_run_spec(
            queued_python_run_spec(1, "source", "copied")
        )
        with pytest.raises(Exception) as exc:
            asyncio.run(backend.run_action("demo", spec))
        assert "unsafe:local_code" not in str(exc.value)


class DerivedPythonParams(ActionParams):
    source: ColumnRef[str]
    expression: str


class DerivedPythonOutput(BaseModel):
    copied: str


async def derived_python(
    params: DerivedPythonParams, row: Row, evaluator: PythonEvaluator
) -> RowResult[DerivedPythonOutput]:
    value = await evaluator.evaluate(
        code=params.expression, row={"value": params.source.read(row)}
    )
    return RowResult(output=DerivedPythonOutput(copied=value))


@pytest.mark.parametrize("custom_params", [False, True])
def test_http_python_capability_refuses_before_execution_when_disabled(
    tmp_path, monkeypatch, custom_params
):
    """Capability admission covers alternate Params and derived evaluator args."""
    from frisket.engine.executor import python_transform
    from frisket.server.app import create_app

    if custom_params:
        custom_action_id = register_typed_action(
            monkeypatch,
            "example",
            action(
                name="derived_python",
                title="Derived Python",
                description="Evaluate selected values through the Python capability.",
                category=ActionCategory.CONVERT,
                run=map_rows(derived_python),
            ),
        )

    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Guard"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("data")
    source_id = project.add_column(sheet_id, "source")
    project.add_rows(sheet_id, [{"source": "hello"}], {"source": source_id})
    request = v1_action_from_canonical_run_spec(
        queued_python_run_spec(sheet_id, "source", "copied")
    )
    if custom_params:
        request.update(
            action_id=custom_action_id,
            params={"source": "source", "expression": "result = row['value']"},
        )
    before_changes = project.db.total_changes
    resolver_calls = []

    def forbidden_resolver():
        resolver_calls.append(True)
        raise AssertionError("disabled Python must not acquire an executor")

    monkeypatch.setattr(python_transform, "resolve_executor", forbidden_resolver)
    monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
    response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=request)

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["errors"][0]["code"] == "code_action_disabled"
    assert "unsafe:local_code" in body["errors"][0]["message"]
    assert resolver_calls == []
    assert project.db.total_changes == before_changes
    assert [column["name"] for column in project.columns(sheet_id)] == ["source"]


class TestBackfillGuardCodeAction:
    def _guard(self):
        from frisket.engine.executor.action_families.runs import (
            _guard_backfill_run_spec,
        )

        return _guard_backfill_run_spec

    def test_backfill_guard_rejects_canonical_map_python(self, monkeypatch):
        # Persisted runner specs use the canonical action kind "map.python".
        monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
        err = self._guard()({"action_kind": "map.python"})
        assert err is not None
        assert err.code == "code_action_disabled"
        assert "unsafe:local_code" in err.message
        assert "trusted/local" in err.message

    def test_backfill_guard_message_does_not_claim_research_answer_runs_code(
        self, monkeypatch
    ):
        # research.answer executes no user code: its tools make model-directed
        # network calls. The message must name that, not "runs code".
        monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
        err = self._guard()({"action_kind": "research.answer"})
        assert err is not None
        assert "runs code" not in err.message
        assert "model:complete" in err.message
        assert "external:web_search" in err.message

    def test_backfill_guard_rejects_live_code_ids(self, monkeypatch):
        monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
        assert self._guard()({"action_kind": "map.mcp_extract"}) is not None
        assert self._guard()({"action_kind": "map.python"}) is not None
        assert self._guard()({"action_kind": "research.answer"}) is not None

    def test_backfill_guard_allows_benign_recipe(self, monkeypatch):
        monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
        assert self._guard()({"action_kind": "map.extract"}) is None
        assert self._guard()({"action_kind": "map.template"}) is None

    def test_backfill_guard_still_allows_research_web_search(self, monkeypatch):
        # the regression the naive "gate external:web_search" rule would cause.
        monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
        assert self._guard()({"action_kind": "research.web_search"}) is None

    def test_backfill_guard_noop_when_code_enabled(self, monkeypatch):
        # the guard only bites when code recipes are disabled; a trusted/local
        # server (FRISKET_ALLOW_CODE_RECIPES=1) is unaffected — no regression.
        monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "1")
        assert self._guard()({"action_kind": "map.python"}) is None
        assert self._guard()({"action_kind": "research.answer"}) is None

    @pytest.mark.parametrize(
        "kind", ["map.mcp_extract", "map.python", "research.answer"]
    )
    def test_backfill_estimate_refuses_disabled_stored_action(self, monkeypatch, kind):
        from frisket.actions.system import typed_action_for_request
        from frisket.engine.executor import run_backfill_action

        monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
        monkeypatch.setattr(
            run_backfill_action,
            "_resolve_backfill_target",
            lambda _project, **_kwargs: {
                "runner_spec": {"action_kind": kind},
            },
        )

        with pytest.raises(run_backfill_action.BackfillRefused) as caught:
            run_backfill_action.prepare_backfill_action(
                object(),
                typed_action_for_request(
                    {
                        "action_id": "run.backfill",
                        "scope": {"kind": "sheet_rows", "sheet_id": 1},
                        "params": {"column": "generated"},
                        "idempotency_key": "disabled-estimate",
                    }
                ),
            )

        assert caught.value.error.code == "code_action_disabled"
