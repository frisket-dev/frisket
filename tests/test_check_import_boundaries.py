from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.check_import_boundaries import (
    ImportBoundaryRule,
    check_import_boundaries,
    _rules_from_json,
)


RULE = ImportBoundaryRule(
    id="fixture-leaf",
    modules=("pkg.leaf",),
    allowed_imports=("pkg.allowed", "pkg.nested.tool"),
    allow_stdlib=True,
)


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def test_checker_reports_exact_diagnostics_for_forbidden_and_unresolved_mutations(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src"
    _write_tree(
        source_root,
        {
            "pkg/__init__.py": "",
            "pkg/allowed.py": "",
            "pkg/hidden.py": "",
            "pkg/leaf.py": "",
            "json.py": "",
        },
    )
    mutations = {
        "import vendor.client as client\n": (
            "fixture-leaf: pkg/leaf.py:1: forbidden import: vendor.client "
            "(import vendor.client as client)"
        ),
        "from . import hidden as alias\n": (
            "fixture-leaf: pkg/leaf.py:1: forbidden import: pkg.hidden "
            "(from . import hidden as alias)"
        ),
        "import importlib as loader\nloader.import_module('vendor.plugin')\n": (
            "fixture-leaf: pkg/leaf.py:2: forbidden import: vendor.plugin "
            "(loader.import_module('vendor.plugin'))"
        ),
        "from importlib import import_module as load\nload(target)\n": (
            "fixture-leaf: pkg/leaf.py:2: unresolved dynamic import: <dynamic> "
            "(load(target))"
        ),
        "__import__(target)\n": (
            "fixture-leaf: pkg/leaf.py:1: unresolved dynamic import: <dynamic> "
            "(__import__(target))"
        ),
        "import json\n": (
            "fixture-leaf: pkg/leaf.py:1: forbidden import: json (import json)"
        ),
    }

    leaf = source_root / "pkg" / "leaf.py"
    for mutation, expected in mutations.items():
        leaf.write_text(mutation, encoding="utf-8")
        diagnostics = tuple(map(str, check_import_boundaries(source_root, (RULE,))))
        assert expected in diagnostics


MEMBER_RULE = ImportBoundaryRule(
    id="fixture-member-owner",
    modules=("pkg.*",),
    exclude=("pkg.cache", "pkg.process_owner"),
    forbidden_members=(
        "pkg.cache.promote",
        "pkg.cache.uninstall",
        "pkg.process_owner.Process._*",
    ),
    reject_unresolved_dynamic=False,
)


@pytest.mark.parametrize(
    "source, forbidden",
    [
        ("from pkg.cache import promote as publish\npublish()\n", "pkg.cache.promote"),
        (
            "from pkg import cache as storage\nstorage.uninstall()\n",
            "pkg.cache.uninstall",
        ),
        ("import pkg.cache as storage\nstorage.promote()\n", "pkg.cache.promote"),
        ("import pkg.cache\npkg.cache.promote()\n", "pkg.cache.promote"),
        ("from . import cache\ncallback = cache.promote\n", "pkg.cache.promote"),
        ("from .cache import uninstall\nuninstall()\n", "pkg.cache.uninstall"),
        ("from pkg.cache import *\n", "pkg.cache.*"),
        (
            "from pkg.process_owner import Process as Child\nChild._cleanup()\n",
            "pkg.process_owner.Process._cleanup",
        ),
    ],
)
def test_member_owner_rejects_aliases_and_callback_escape(
    tmp_path: Path, source: str, forbidden: str
) -> None:
    _write_tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/cache.py": "def promote(): pass\n",
            "pkg/process_owner.py": "",
            "pkg/caller.py": source,
        },
    )
    violations = check_import_boundaries(tmp_path, (MEMBER_RULE,))
    assert any(
        item.imported == forbidden and item.reason == "forbidden member access"
        for item in violations
    )


def test_member_owner_allows_reads_public_commands_and_owner_implementation(
    tmp_path: Path,
) -> None:
    _write_tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/cache.py": "from pkg import cache\ndef promote(): cache.promote()\n",
            "pkg/process_owner.py": "class Process:\n def _cleanup(self): pass\n",
            "pkg/caller.py": (
                "from pkg import cache\nfrom pkg.process_owner import Process\n"
                "cache.is_installed()\nProcess.start()\n"
                "# cache.promote() is documentation, not a use.\n"
                "example = 'pkg.cache.uninstall()'\n"
                "def promote(): pass\n"
            ),
        },
    )
    assert not check_import_boundaries(tmp_path, (MEMBER_RULE,))


def _write_purchase_tree(root: Path, files: dict[str, str]) -> None:
    namespaces = ("actions", "sdk", "engine/executor", "ops")
    baseline = {"frisket/__init__.py": ""}
    for namespace in namespaces:
        baseline[f"frisket/{namespace}/__init__.py"] = ""
        baseline[f"frisket/{namespace}/fixture.py"] = ""
    _write_tree(root, {**baseline, **files})


PURCHASE_RULE_IDS = {
    "model-provider-transports-have-finite-owners",
    "sidecar-raw-transport-is-private",
    "local-purchase-policy-belongs-to-composition",
    "model-router-construction-belongs-to-composition",
    "operator-credential-values-belong-to-transport-leaves",
    "sidecar-credential-values-belong-to-transport-leaves",
    "pydantic-agent-construction-belongs-to-router-bridges",
}


def _purchase_rules() -> tuple[ImportBoundaryRule, ...]:
    config = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "ci"
        / "import_boundaries.json"
    )
    rules = tuple(
        rule for rule in _rules_from_json(config) if rule.id in PURCHASE_RULE_IDS
    )
    assert {rule.id for rule in rules} == PURCHASE_RULE_IDS
    return rules


@pytest.mark.parametrize(
    "caller", ["actions/new_action", "sdk/new_client", "engine/executor/new_reader"]
)
@pytest.mark.parametrize(
    "source, expected_rule",
    [
        (
            "from frisket.ai.llm.adapters import AnthropicAdapter as Provider\nProvider('key')\n",
            "model-provider-transports-have-finite-owners",
        ),
        (
            "import frisket.ai.llm.adapters as wire\nwire.OpenAICompatAdapter('key')\n",
            "model-provider-transports-have-finite-owners",
        ),
        (
            "from frisket.ai import llm as models\ntransport = models.adapters.AnthropicAdapter\n",
            "model-provider-transports-have-finite-owners",
        ),
        (
            "from importlib import import_module\nwire = import_module('frisket.ai.llm.adapters')\n",
            "model-provider-transports-have-finite-owners",
        ),
        (
            "from openai import AsyncOpenAI\n",
            "model-provider-transports-have-finite-owners",
        ),
        (
            "from pydantic_ai import Agent as Assistant\nAssistant('openai:unowned')\n",
            "pydantic-agent-construction-belongs-to-router-bridges",
        ),
        (
            "from pydantic_ai.agent import Agent as Assistant\nAssistant[None, str]('openai:unowned')\n",
            "pydantic-agent-construction-belongs-to-router-bridges",
        ),
        (
            "from pydantic_ai.models.openai import OpenAIChatModel\n",
            "model-provider-transports-have-finite-owners",
        ),
        (
            "from frisket.ops._sidecar import _sidecar_post_transport as send\n",
            "sidecar-raw-transport-is-private",
        ),
        (
            "from frisket.ops import _sidecar as models\ncallback = models._sidecar_post_transport\n",
            "sidecar-raw-transport-is-private",
        ),
        (
            "from frisket.ai.llm import ModelRouter as Router\nRouter()\n",
            "model-router-construction-belongs-to-composition",
        ),
        (
            "from frisket.ai.llm import *\n",
            "model-router-construction-belongs-to-composition",
        ),
        (
            "from frisket.credentials import resolve_credential as key\n",
            "operator-credential-values-belong-to-transport-leaves",
        ),
        (
            "import frisket.credentials as secrets\ncallback = secrets.resolve_credential_with_source\n",
            "operator-credential-values-belong-to-transport-leaves",
        ),
        (
            "from frisket import credentials\ncredentials.resolve_credential_for_use(project, 'KEY', context=None)\n",
            "operator-credential-values-belong-to-transport-leaves",
        ),
        (
            "from frisket.ops._sidecar import sidecar_token\n",
            "sidecar-credential-values-belong-to-transport-leaves",
        ),
        (
            "from frisket.ops import _sidecar as models\nmodels.sidecar_headers()\n",
            "sidecar-credential-values-belong-to-transport-leaves",
        ),
        (
            "import frisket.ops._sidecar as models\nconnection = models.ephemeral_gateway_connection(op='ocr')\n",
            "sidecar-credential-values-belong-to-transport-leaves",
        ),
        (
            "import frisket.ai.llm.router as models\nmodels.ModelRouter()\n",
            "model-router-construction-belongs-to-composition",
        ),
        (
            "from frisket.execution.purchases import LocalPurchasePolicy as Free\nFree()\n",
            "local-purchase-policy-belongs-to-composition",
        ),
        (
            "import frisket.execution.purchases as purchase\npolicy = purchase.LOCAL_PURCHASE_POLICY\n",
            "local-purchase-policy-belongs-to-composition",
        ),
    ],
)
def test_new_authoring_and_executor_callers_cannot_borrow_raw_model_transport(
    tmp_path: Path,
    caller: str,
    source: str,
    expected_rule: str,
) -> None:
    _write_purchase_tree(
        tmp_path,
        {
            "frisket/__init__.py": "",
            "frisket/actions/__init__.py": "",
            "frisket/sdk/__init__.py": "",
            "frisket/engine/executor/__init__.py": "",
            "frisket/ops/__init__.py": "",
            f"frisket/{caller}.py": source,
        },
    )
    violations = check_import_boundaries(tmp_path, _purchase_rules())
    assert any(violation.rule_id == expected_rule for violation in violations)


def test_purchase_fences_allow_real_transport_owners_public_clients_and_ordinary_http(
    tmp_path: Path,
) -> None:
    _write_purchase_tree(
        tmp_path,
        {
            "frisket/__init__.py": "",
            "frisket/ai/llm/router.py": (
                "from frisket.ai.llm.adapters import AnthropicAdapter\n"
                "from frisket.ai.llm.cloud import CloudLLMAdapter\n"
                "from frisket.execution.purchases import LOCAL_PURCHASE_POLICY\n"
            ),
            "frisket/ops/_sidecar.py": "from frisket.ops._sidecar import _sidecar_post_transport\n",
            "frisket/execution/purchases.py": "from frisket.execution.purchases import LocalPurchasePolicy\n",
            "frisket/server/workspace.py": "from frisket.ai.llm import ModelRouter\nrouter = ModelRouter()\n",
            "frisket/engine/jobs/runs.py": "from frisket.ai.llm.router import ModelRouter as Router\nrouter = Router()\n",
            "frisket/actions/new_action.py": (
                "import httpx\n"
                "from frisket.ai.llm import ModelRouter\ndef use_client(router: ModelRouter): pass\n"
                "from frisket.ops._sidecar import sidecar_post, probe_sidecar_capabilities, SidecarHTTPError\n"
                "from frisket.ai.llm.types import LLMRequest, LLMResponse\n"
                "from pydantic_ai.models import Model\n"
                "from pydantic_ai import Agent\ndef inspect_agent(agent: Agent): pass\n"
                "from frisket.credentials import ResolvedCredential, missing_required_credentials, resolve_project_secret\n"
            ),
        },
    )
    assert not check_import_boundaries(tmp_path, _purchase_rules())


def test_router_constructor_fence_resolves_relative_imports_without_banning_annotations(
    tmp_path: Path,
) -> None:
    caller = tmp_path / "frisket" / "actions" / "new_action.py"
    _write_purchase_tree(
        tmp_path,
        {
            "frisket/__init__.py": "",
            "frisket/actions/new_action.py": (
                "from ..ai.llm.router import ModelRouter as Router\n"
                "def use_client(router: Router): pass\n"
            ),
        },
    )
    assert not check_import_boundaries(tmp_path, _purchase_rules())
    with caller.open("a") as stream:
        stream.write("router = Router()\n")
    violations = check_import_boundaries(tmp_path, _purchase_rules())
    assert any(
        item.rule_id == "model-router-construction-belongs-to-composition"
        and item.imported == "frisket.ai.llm.router.ModelRouter"
        for item in violations
    )


@pytest.mark.parametrize(
    "owner",
    [
        "frisket.engine.executor.document_convert",
        "frisket.engine.executor.decision_read",
        "frisket.engine.executor.geocode_capability",
        "frisket.engine.executor.census_capability",
        "frisket.sdk.ops.transcription.hosted",
        "frisket.ops.ocr_engines_hosted",
        "frisket.ops.opendocrouter",
        "frisket.ops.integrations.translation_engine",
    ],
)
def test_credential_fence_allows_only_the_existing_named_purchase_transport_leaves(
    tmp_path: Path, owner: str
) -> None:
    _write_purchase_tree(
        tmp_path,
        {
            "frisket/__init__.py": "",
            "frisket/actions/__init__.py": "",
            "frisket/sdk/__init__.py": "",
            "frisket/engine/executor/__init__.py": "",
            "frisket/ops/__init__.py": "",
            owner.replace(".", "/")
            + ".py": "from frisket.credentials import resolve_credential_for_use\n",
        },
    )
    assert not check_import_boundaries(tmp_path, _purchase_rules())


@pytest.mark.parametrize(
    "owner",
    [
        "frisket.ai.llm.structured",
        "frisket.ai.research.row_answer",
        "frisket.ops.mcp_extract",
        "frisket.server.services.project_qa_runner",
    ],
)
def test_only_existing_router_bridges_construct_pydantic_agents(
    tmp_path: Path, owner: str
) -> None:
    _write_purchase_tree(
        tmp_path,
        {
            owner.replace(".", "/")
            + ".py": "from pydantic_ai import Agent\nagent = Agent(model)\n",
        },
    )
    assert not check_import_boundaries(tmp_path, _purchase_rules())
