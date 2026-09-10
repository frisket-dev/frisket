from __future__ import annotations

import ast
import importlib
import importlib.util
import re
from dataclasses import fields
from pathlib import Path
from types import ModuleType
from typing import Any, get_args

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CONTRACTS_ROOT = SRC / "frisket" / "contracts"
BASE_MODULE = "frisket.contracts.http.endpoint_catalog"

BASE_REQUIRED_FIELDS = (
    "id",
    "route_owner",
    "route_name",
    "method",
    "auth",
    "project_role",
)
AUTH_MODES = frozenset({"public", "session_or_pat", "browser_session", "admin"})
PROJECT_ROLES = frozenset({"viewer", "reviewer", "editor", "owner"})
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
COMMERCE_RESOLVERS = frozenset({"paid.account", "funding.reservation"})
PRIVATE_ONLY_ID = re.compile(
    r"checkout|stripe_webhook|funding|signup_request|admin_create_org"
    r"|tenant_dispatch_route"
)
COMPILER_PARAMETERS = frozenset({"declarations", "registered_routes", "resolvers"})

pytestmark = pytest.mark.gap


# ---------------------------------------------------------------------------
# guarded loaders (absence must be a semantic red, never a collection error)
# ---------------------------------------------------------------------------


def _load_module(name: str, purpose: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except Exception as exc:  # an import crash is a product-contract failure
        pytest.fail(
            f"INTENDED_SPLIT_RED: {purpose} module {name} is not importable ({exc})",
            pytrace=False,
        )


def _declaration_shaped(candidate: object) -> bool:
    return all(hasattr(candidate, field) for field in BASE_REQUIRED_FIELDS) and (
        isinstance(getattr(candidate, "id", None), str)
    )


def _base_catalog_entries() -> tuple[Any, ...]:
    module = _load_module(BASE_MODULE, "neutral base endpoint catalog")
    candidates = sorted(
        name
        for name, value in vars(module).items()
        if not name.startswith("_")
        and isinstance(value, tuple)
        and value
        and all(_declaration_shaped(item) for item in value)
    )
    if len(candidates) != 1:
        pytest.fail(
            "INTENDED_SPLIT_RED: the base catalog module must export exactly "
            "one public nonempty tuple of endpoint declarations (fields "
            f"{BASE_REQUIRED_FIELDS}); found candidates: {candidates}",
            pytrace=False,
        )
    return getattr(module, candidates[0])


def _identity(entry: Any) -> tuple[str, str, str]:
    return (entry.route_owner, entry.route_name, entry.method)


def _module_name_for(path: Path) -> tuple[str, str]:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    name = ".".join(parts)
    package = name if is_package else name.rpartition(".")[0]
    return name, package


def _locate_single_policy_compiler() -> Any:
    matches: list[tuple[Path, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        # rule19: executes-the-artifact — scan only locates the single policy compiler, which the tests then import and drive
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parameters = {
                    argument.arg
                    for argument in (
                        *node.args.posonlyargs,
                        *node.args.args,
                        *node.args.kwonlyargs,
                    )
                }
                if COMPILER_PARAMETERS <= parameters:
                    matches.append((path, node.name))
    if len(matches) != 1:
        pytest.fail(
            "INTENDED_SPLIT_RED: exactly one module-level policy compiler "
            "accepting (declarations, registered_routes, resolvers) must "
            "exist under src/frisket (one compiler, per-edition inputs); "
            "found: "
            + ", ".join(f"{path.relative_to(ROOT)}:{name}" for path, name in matches),
            pytrace=False,
        )
    path, symbol = matches[0]
    module_name, _ = _module_name_for(path)
    module = _load_module(module_name, "policy compiler")
    return getattr(module, symbol)


def _registered_row(entry: Any, index: int) -> dict[str, str]:
    return {
        "route_owner": entry.route_owner,
        "route_name": entry.route_name,
        "method": entry.method,
        "route_template": f"/api/catalog-probe/{entry.route_owner}/{index}",
    }


# ---------------------------------------------------------------------------
# 1. neutral base catalog exists, is neutral, and is a stdlib import leaf
# ---------------------------------------------------------------------------


def test_base_endpoint_catalog_exists_and_is_commerce_neutral() -> None:
    module = _load_module(BASE_MODULE, "neutral base endpoint catalog")
    entries = _base_catalog_entries()

    assert get_args(module.RouteSpec) == (str, str), (
        "endpoint RouteSpec must contain only (route_name, method)"
    )
    assert "path" not in {field.name for field in fields(module.EndpointPolicy)}
    assert "path" not in {field.name for field in fields(module.RoutePolicyDecision)}
    identities: set[tuple[str, str, str]] = set()
    for entry in entries:
        identity = _identity(entry)
        assert identity not in identities, (
            "INTENDED_SPLIT_RED: duplicate base endpoint identity "
            f"{identity!r}: the base catalog must declare each shared "
            "(owner, name, method) exactly once"
        )
        identities.add(identity)
        assert isinstance(entry.id, str) and entry.id, (
            f"INTENDED_SPLIT_RED: base entry {identity!r} needs a nonempty id"
        )
        assert isinstance(entry.route_owner, str) and entry.route_owner, (
            f"INTENDED_SPLIT_RED: base entry {entry.id!r} needs a route_owner"
        )
        assert isinstance(entry.route_name, str) and entry.route_name, (
            f"INTENDED_SPLIT_RED: base entry {entry.id!r} needs a route_name"
        )
        assert entry.method in METHODS, (
            f"INTENDED_SPLIT_RED: base entry {entry.id!r} has unsupported "
            f"method {entry.method!r}"
        )
        assert not hasattr(entry, "path"), (
            f"base entry {entry.id!r} must not copy the registered route path"
        )
        assert entry.auth in AUTH_MODES, (
            f"INTENDED_SPLIT_RED: base entry {entry.id!r} has unsupported "
            f"auth mode {entry.auth!r}"
        )
        assert entry.project_role is None or entry.project_role in PROJECT_ROLES, (
            f"INTENDED_SPLIT_RED: base entry {entry.id!r} has unsupported "
            f"project_role {entry.project_role!r}"
        )

        # Neutrality: the PUBLIC base catalog carries no commerce effects and
        # no private-only endpoint declarations.
        resolvers = tuple(getattr(entry, "resolvers", ()) or ())
        commerce = sorted(set(resolvers) & COMMERCE_RESOLVERS)
        assert not commerce, (
            f"INTENDED_SPLIT_RED: base entry {entry.id!r} publishes commerce "
            f"resolvers {commerce}; paid.account/funding.reservation belong "
            "to an external managed contribution"
        )
        assert not getattr(entry, "reserves_funding", False), (
            f"INTENDED_SPLIT_RED: base entry {entry.id!r} sets "
            "reserves_funding; funding reservation is an external managed effect"
        )
        assert not PRIVATE_ONLY_ID.search(entry.id), (
            f"INTENDED_SPLIT_RED: base entry {entry.id!r} is a private-only "
            "endpoint (checkout/stripe_webhook/funding/signup_request/"
            "admin_create_org/tenant_dispatch_route); it must live only in "
            "an external managed contribution"
        )


# ---------------------------------------------------------------------------
# 2. contracts package no longer imports private composition
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 3. hosted contribution consumes the base catalog without redeclaring it
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 4. parity: composed hosted catalog reproduces the frozen snapshot exactly
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5. one fail-closed per-edition compiler
# ---------------------------------------------------------------------------


def test_declare_endpoints_accepts_pairs_and_rejects_legacy_path_shapes() -> None:
    module = _load_module(BASE_MODULE, "neutral base endpoint catalog")

    declared = module.declare_endpoints(
        "tenant",
        "public",
        (("probe", "GET"),),
    )
    assert len(declared) == 1
    assert _identity(declared[0]) == ("tenant", "probe", "GET")
    assert not hasattr(declared[0], "path")

    with pytest.raises((TypeError, ValueError)):
        module.declare_endpoints(
            "tenant",
            "public",
            (("probe", "GET", "/api/probe"),),
        )
    for legacy_key in ("path", "route_template", "template"):
        with pytest.raises((TypeError, ValueError)):
            module.declare_endpoints(
                "tenant",
                "public",
                (
                    {
                        "route_name": "probe",
                        "method": "GET",
                        legacy_key: "/api/probe",
                    },
                ),
            )


def test_policy_compiler_is_single_sourced_exhaustive_and_fail_closed() -> None:
    compiler = _locate_single_policy_compiler()

    declaration = {
        "route_owner": "tenant",
        "route_name": "probe",
        "method": "GET",
        "auth": "session_or_pat",
    }
    registered = {
        "route_owner": "tenant",
        "route_name": "probe",
        "method": "GET",
        "route_template": "/api/probe",
    }

    compiled = compiler(
        declarations=[declaration], registered_routes=[registered], resolvers={}
    )
    assert compiled is not None, "a coherent policy composition must compile"

    resolve = getattr(compiled, "resolve", None)
    assert callable(resolve), (
        "INTENDED_SPLIT_RED: the compiled policy must expose a fail-closed "
        "resolve(route_owner, route_name, method) lookup"
    )
    decision = resolve("tenant", "probe", "GET")
    assert getattr(decision, "auth", None) == "session_or_pat", (
        "INTENDED_SPLIT_RED: compiled decision must carry the declared auth"
    )
    with pytest.raises(Exception):
        resolve("tenant", "unknown_probe", "GET")

    # Duplicate/ambiguous declaration identity is rejected.
    with pytest.raises(Exception):
        compiler(
            declarations=[declaration, dict(declaration)],
            registered_routes=[registered],
            resolvers={},
        )

    # A duplicate registered identity is ambiguous even if its rows agree.
    with pytest.raises(Exception):
        compiler(
            declarations=[declaration],
            registered_routes=[registered, dict(registered)],
            resolvers={},
        )

    # A registered route without a declaration is rejected (exhaustive).
    orphan_route = {
        "route_owner": "tenant",
        "route_name": "orphan",
        "method": "GET",
        "route_template": "/api/orphan",
    }
    with pytest.raises(Exception):
        compiler(
            declarations=[declaration],
            registered_routes=[registered, orphan_route],
            resolvers={},
        )

    # A declaration without a registered route is rejected.
    orphan_declaration = dict(declaration, route_name="orphan")
    with pytest.raises(Exception):
        compiler(
            declarations=[declaration, orphan_declaration],
            registered_routes=[registered],
            resolvers={},
        )

    # A declared effect without its resolver is rejected; with it, compiles.
    effectful = dict(declaration, resolvers=("probe.effect",))
    with pytest.raises(Exception):
        compiler(declarations=[effectful], registered_routes=[registered], resolvers={})
    compiled_with_effect = compiler(
        declarations=[effectful],
        registered_routes=[registered],
        resolvers={"probe.effect": lambda **_: None},
    )
    assert compiled_with_effect is not None

    # Registered paths remain validated, but declarations cannot carry any
    # legacy spelling of that live route fact.
    with pytest.raises(Exception):
        compiler(
            declarations=[declaration],
            registered_routes=[dict(registered, route_template="probe")],
            resolvers={},
        )
    for legacy_key in ("path", "route_template", "template"):
        with pytest.raises(Exception):
            compiler(
                declarations=[dict(declaration, **{legacy_key: "/api/probe"})],
                registered_routes=[registered],
                resolvers={},
            )

    # A registered path may move without changing or enriching the compiled
    # policy decision.
    moved = compiler(
        declarations=[declaration],
        registered_routes=[dict(registered, route_template="/api/moved-probe")],
        resolvers={},
    ).resolve("tenant", "probe", "GET")
    assert not hasattr(moved, "path")
    assert moved.auth == "session_or_pat"

    # One owner cannot register two identities on the same live wire.
    second_declaration = dict(declaration, route_name="second_probe")
    second_registered = dict(registered, route_name="second_probe")
    with pytest.raises(Exception):
        compiler(
            declarations=[declaration, second_declaration],
            registered_routes=[registered, second_registered],
            resolvers={},
        )

    # Distinct owners may legitimately expose the same method/path.
    outer_declaration = dict(
        declaration,
        route_owner="outer",
        route_name="outer_probe",
    )
    outer_registered = dict(
        registered,
        route_owner="outer",
        route_name="outer_probe",
    )
    cross_owner = compiler(
        declarations=[declaration, outer_declaration],
        registered_routes=[registered, outer_registered],
        resolvers={},
    )
    assert cross_owner.resolve("tenant", "probe", "GET").auth == "session_or_pat"
    assert cross_owner.resolve("outer", "outer_probe", "GET").auth == "session_or_pat"


def test_team_edition_composition_fails_closed_on_private_only_route() -> None:
    base_entries = _base_catalog_entries()
    compiler = _locate_single_policy_compiler()

    base_registered = [
        _registered_row(entry, index) for index, entry in enumerate(base_entries)
    ]
    base_resolvers = {
        resolver_id: (lambda **_: None)
        for entry in base_entries
        for resolver_id in tuple(getattr(entry, "resolvers", ()) or ())
    }

    # Positive control first: the base contribution alone is a coherent
    # team-edition composition over exactly its own routes.
    compiled = compiler(
        declarations=list(base_entries),
        registered_routes=base_registered,
        resolvers=base_resolvers,
    )
    assert compiled is not None, (
        "INTENDED_SPLIT_RED: base-only (team-edition) declarations must "
        "compile against exactly the base route inventory"
    )

    # Fail closed: a registered route that only the private contribution
    # declares must refuse to compile in a base-only composition.
    private_route = {
        "route_owner": "outer",
        "route_name": "checkout",
        "method": "POST",
        "route_template": "/api/billing/checkout",
    }
    with pytest.raises(
        Exception, match=r"(?i)unclassified|missing|unregistered|checkout"
    ):
        compiler(
            declarations=list(base_entries),
            registered_routes=[*base_registered, private_route],
            resolvers=base_resolvers,
        )
