"""Closed-world inventory of production project-run terminal writes.

The project-run terminalizer joins several durable records: the run, its v1
receipt, the execution attempt, and (where the action family has one) the
output claim.  The historical failure mode was counting those writers by
memory and overlooking one bespoke action family.  This file instead walks
the whole production package and freezes every call to the run-status
primitives, every direct ``UPDATE runs SET status`` statement, every shared
``terminalize_project_run`` entry, and every call to the runner's
in-transaction finalizer.

Function-qualified counts are deliberate.  They survive harmless line moves,
but a second write added to an already-known function still changes the
inventory and turns the test red.  Entries outside the composite family carry
their reason here rather than disappearing from the denominator.

The runless ``action.run`` receipt path is a separate inventory below.  It has
no run or attempt to fence and must keep its monotonic return-on-terminal
behavior; grouping it with the project-run kernel would assert a tuple that
does not exist.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src" / "frisket"

SiteKey = tuple[str, str, str]
Declaration = tuple[int, str]

_TERMINAL_CALL_NAMES = {
    "_terminalize_v1_queued_receipt",
    "_finalize_run_in_transaction",
    "finish_run",
    "finish_running_run",
    "queued_v1_terminal_failure_result",
    "queued_v1_terminal_receipt_result",
    "queued_v1_terminal_receipt_transition",
    "terminalize_project_run",
}
_RAW_RUN_STATUS_UPDATE = re.compile(
    r"\bUPDATE\s+runs\s+SET\b(?:(?!\bWHERE\b).)*\bstatus\s*=",
    flags=re.IGNORECASE | re.DOTALL,
)
_RAW_RUN_HALT_UPDATE = re.compile(
    r"\bUPDATE\s+runs\s+SET\b(?:(?!\bWHERE\b).)*\bhalted_(?:code|reason)\s*=",
    flags=re.IGNORECASE | re.DOTALL,
)
_HALT_KEYS = {"halted_code", "halted_reason"}


@dataclass(frozen=True)
class _Site:
    key: SiteKey
    line: int
    source_line: str


# Every production entry into the shared run/receipt/attempt/claim kernel.
# Keeping this separate from the legacy/family-specific terminal surfaces
# makes a newly-routed family an explicit inventory change.
PROJECT_RUN_KERNEL_ENTRIES: dict[SiteKey, Declaration] = {
    (
        "src/frisket/engine/executor/action_lifecycle.py",
        "_run_reserved_maprunner_action",
        "terminalize_project_run",
    ): (
        1,
        "Evidence publication failure retains paid facts and its failed receipt "
        "through the kernel's exact current-writer/claim fence; reserved sibling "
        "effects refuse settlement and retain reconciliation authority.",
    ),
    (
        "src/frisket/engine/executor/cluster_action.py",
        "finalize_cluster_action.abort",
        "terminalize_project_run",
    ): (
        1,
        "Permanent clustering publication failures abort through the shared "
        "kernel under the admitted writer and output claim, without sealing results.",
    ),
    (
        "src/frisket/engine/executor/semantic_join_action.py",
        "finalize_semantic_join.abort",
        "terminalize_project_run",
    ): (
        1,
        "Permanent semantic admission failures use current writer/claim authority "
        "to abort through the shared kernel without publishing deferred results.",
    ),
    (
        "src/frisket/engine/executor/action_families/embeddings.py",
        "_do_refresh",
        "terminalize_project_run",
    ): (
        1,
        "Embedding transaction B joins its claimless terminal tuple to the "
        "shared kernel after checkpoint retirement.",
    ),
    (
        "src/frisket/engine/executor/group_summary_runtime.py",
        "_write_reduce_group_summary_result",
        "terminalize_project_run",
    ): (
        1,
        "Reduce summary joins run, receipt, attempt, and claimless checkpoint "
        "state through the shared kernel.",
    ),
    (
        "src/frisket/engine/executor/queued_actions.py",
        "queued_v1_terminal_receipt_transition",
        "terminalize_project_run",
    ): (
        1,
        "Queued project-run workers, cancellation, and recovery converge at "
        "this single shared-kernel entry.",
    ),
}


# Public/adaptor entries that eventually reach the one queued kernel call.
# Scanning these as well as the low-level primitive prevents a new caller from
# hiding behind an existing wrapper while leaving the primitive count stable.
PROJECT_RUN_TERMINAL_API_ENTRIES: dict[SiteKey, Declaration] = {
    (
        "src/frisket/engine/executor/action_reservations.py",
        "_recover_existing_queued_action_reservation",
        "queued_v1_terminal_receipt_result",
    ): (
        1,
        "Typed queue recovery closes a prepared run through the shared terminal "
        "kernel when its frozen source descriptors have changed.",
    ),
    (
        "src/frisket/engine/executor/queued_actions.py",
        "_terminalize_undecodable_queued_payload",
        "queued_v1_terminal_receipt_result",
    ): (1, "Undecodable queued payloads enter the shared failure terminal path."),
    (
        "src/frisket/engine/executor/queued_actions.py",
        "queued_v1_terminal_receipt_result",
        "queued_v1_terminal_receipt_transition",
    ): (1, "The compatibility result wrapper delegates to the typed transition."),
    (
        "src/frisket/engine/executor/queued_actions.py",
        "queued_v1_terminal_failure_result",
        "queued_v1_terminal_receipt_result",
    ): (1, "The failure convenience wrapper delegates to receipt terminalization."),
    (
        "src/frisket/engine/executor/queued_actions.py",
        "queued_v1_worker_exception_failure_result",
        "queued_v1_terminal_receipt_result",
    ): (1, "Malformed worker exceptions still enter shared receipt terminalization."),
    (
        "src/frisket/engine/executor/queued_actions.py",
        "queued_v1_worker_exception_failure_result",
        "queued_v1_terminal_failure_result",
    ): (1, "Decoded worker exceptions use the named failure terminal wrapper."),
    (
        "src/frisket/engine/jobs/runs.py",
        "register_project_run_handler.handle",
        "queued_v1_terminal_failure_result",
    ): (2, "The queued worker has two reviewed failure terminal entry branches."),
    (
        "src/frisket/engine/jobs/runs.py",
        "register_project_run_handler.handle",
        "queued_v1_terminal_receipt_result",
    ): (
        2,
        "Worker preparation refusal and cooperative cancellation retain owner authority.",
    ),
    (
        "src/frisket/server/run_status.py",
        "_terminalize_v1_queued_receipt",
        "queued_v1_terminal_receipt_transition",
    ): (1, "Server reconciliation adapts to the typed queued terminal transition."),
    (
        "src/frisket/server/run_status.py",
        "reconcile_project_run_status",
        "_terminalize_v1_queued_receipt",
    ): (4, "Queue failure, cancel, timeout, and orphan status branches are explicit."),
    (
        "src/frisket/server/run_status.py",
        "cancel_project_run",
        "_terminalize_v1_queued_receipt",
    ): (1, "Administrative cancellation resolves through the shared server adapter."),
}


# The kernel's one guarded run-status write is inventoried independently from
# its callers. A second raw write inside the implementation must turn red.
PROJECT_RUN_KERNEL_IMPLEMENTATION: dict[SiteKey, Declaration] = {
    (
        "src/frisket/engine/executor/project_run_terminalization.py",
        "terminalize_project_run",
        "raw_runs_status_sql",
    ): (
        1,
        "The shared kernel owns one observed-source run status CAS, with an "
        "exact owner guard for current-writer reclassification.",
    ),
}


# These functions own or enter intentional project-run terminal transitions
# outside the shared receipt kernel. Every count is the number of matching
# write/entry calls in that function.
PROJECT_RUN_TERMINAL_SURFACES: dict[SiteKey, Declaration] = {
    (
        "src/frisket/engine/executor/action_families/embeddings.py",
        "_do_refresh",
        "finish_run",
    ): (
        1,
        "Embedding keeps one in-process failure fallback after the explicitly "
        "non-transactional vector sidecar boundary.",
    ),
    (
        "src/frisket/engine/jobs/runs.py",
        "register_project_run_handler.handle",
        "finish_run",
    ): (
        2,
        "The project.run worker retains two post-run cleanup writes. Its "
        "pre-dispatch refusal branches enter the shared kernel directly so "
        "run and receipt cannot split across separate commits.",
    ),
    (
        "src/frisket/engine/runner/finalization.py",
        "_finalize_run_in_transaction",
        "finish_run",
    ): (
        1,
        "MapRunner's shared in-transaction finalizer owns its result and run "
        "terminal write under the dispatched-writer entry fence.",
    ),
    (
        "src/frisket/engine/runner/finalization.py",
        "finalize_dispatched_run",
        "_finalize_run_in_transaction",
    ): (
        1,
        "The dispatched runner entry establishes current-writer authority "
        "before entering the in-transaction finalizer.",
    ),
    (
        "src/frisket/engine/runner/finalization.py",
        "finalize_pre_dispatch_run",
        "_finalize_run_in_transaction",
    ): (
        1,
        "The pre-dispatch runner entry is the explicitly claimless counterpart "
        "to dispatched finalization.",
    ),
    (
        "src/frisket/engine/executor/action_lifecycle.py",
        "_terminalize_unclaimed_prepared_run",
        "_finalize_run_in_transaction",
    ): (
        1,
        "Prepared-run refusal enters the same runner transaction before an "
        "attempt can claim dispatched-writer authority.",
    ),
}


# The AST scan is intentionally broader than the kernel family.  These are
# exact, reviewed exceptions rather than silently ignored matches.
EXPLAINED_NON_KERNEL_SITES: dict[SiteKey, Declaration] = {
    (
        "src/frisket/engine/executor/action_families/embeddings.py",
        "_run_paid_embedding_dimension_probe",
        "finish_run",
    ): (
        6,
        "The dimension probe uses a private probe run whose provisional "
        "receipt is deleted on refusal; it is not the enclosing action's "
        "project-run terminal tuple.",
    ),
    (
        "src/frisket/engine/executor/action_families/embeddings.py",
        "_create_index",
        "finish_run",
    ): (
        3,
        "Index creation may own the private dimension-probe run and closes that "
        "probe beside the enclosing index-create transaction.",
    ),
    (
        "src/frisket/engine/store/runs.py",
        "RunResultStore.finish_run",
        "raw_runs_status_sql",
    ): (
        1,
        "This is the generic storage primitive itself; every production caller "
        "is independently counted by this closure.",
    ),
    (
        "src/frisket/engine/store/runs.py",
        "RunResultStore.begin_run_resume",
        "raw_runs_status_sql",
    ): (
        1,
        "Resume admission reopens a terminal run to running and is deliberately "
        "caught by the conservative raw status-write scan.",
    ),
    (
        "src/frisket/engine/store/runs.py",
        "RunResultStore.revert_run_resume",
        "raw_runs_status_sql",
    ): (
        1,
        "Resume rollback restores the snapshotted prior state and is not a new "
        "terminal outcome, but remains visible in the status-write inventory.",
    ),
}


# ``queued_action_job`` receipts have run_id=None.  Freeze their private entry
# calls separately so a new route into that receipt-only terminalizer is also
# reviewed without pretending it has a project-run writer fence.
RUNLESS_RECEIPT_TERMINAL_ENTRIES: dict[SiteKey, Declaration] = {
    (
        "src/frisket/engine/executor/action_jobs.py",
        "action_job_cancelled_result",
        "_terminalize_action_job_receipt",
    ): (
        1,
        "Runless cancellation finalizes the reserved receipt and claim and "
        "returns an existing terminal receipt monotonically.",
    ),
    (
        "src/frisket/engine/executor/action_jobs.py",
        "action_job_failure_result",
        "_terminalize_action_job_receipt",
    ): (
        1,
        "Runless failure finalizes only the receipt/claim/checkpoint tuple; "
        "there is no project run or execution attempt.",
    ),
    (
        "src/frisket/engine/executor/action_jobs.py",
        "action_job_success_result",
        "_terminalize_action_job_receipt",
    ): (
        1,
        "Runless success uses the same receipt-only monotonic terminalizer.",
    ),
}


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _function_name(stack: list[str]) -> str:
    return ".".join(stack) if stack else "<module>"


class _TerminalWriteVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        relative_path: str,
        source: str,
        call_names: set[str],
        docstrings: set[int],
        include_raw_status: bool,
    ) -> None:
        self.relative_path = relative_path
        self.source = source
        self.source_lines = source.splitlines()
        self.call_names = call_names
        self.docstrings = docstrings
        self.include_raw_status = include_raw_status
        self.call_aliases: dict[str, str] = {}
        self.stack: list[str] = []
        self.sites: list[_Site] = []

    def _record(self, marker: str, node: ast.AST) -> None:
        line = node.lineno
        self.sites.append(
            _Site(
                key=(
                    self.relative_path,
                    _function_name(self.stack),
                    marker,
                ),
                line=line,
                source_line=self.source_lines[line - 1].strip(),
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for imported in node.names:
            if imported.name in self.call_names:
                self.call_aliases[imported.asname or imported.name] = imported.name

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        if isinstance(node.func, ast.Name) and name is not None:
            name = self.call_aliases.get(name, name)
        if name in self.call_names:
            self._record(name, node)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if (
            self.include_raw_status
            and id(node) not in self.docstrings
            and isinstance(node.value, str)
            and _RAW_RUN_STATUS_UPDATE.search(node.value)
        ):
            self._record("raw_runs_status_sql", node)


class _HaltWriteVisitor(ast.NodeVisitor):
    def __init__(self, *, relative_path: str) -> None:
        self.relative_path = relative_path
        self.stack: list[str] = []
        self.column_writers: set[tuple[str, str]] = set()
        self.params_mutators: set[tuple[str, str]] = set()
        self.params_update_callers: set[tuple[str, str]] = set()

    def _function_key(self) -> tuple[str, str]:
        return self.relative_path, _function_name(self.stack)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and _RAW_RUN_HALT_UPDATE.search(node.value):
            self.column_writers.add(self._function_key())

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value in _HALT_KEYS
            ):
                self.params_mutators.add(self._function_key())
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        if any(
            isinstance(key, ast.Constant) and key.value in _HALT_KEYS
            for key in node.keys
        ):
            self.params_mutators.add(self._function_key())
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        if name == "update_run_params":
            self.params_update_callers.add(self._function_key())
        if (
            name == "pop"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in _HALT_KEYS
        ):
            self.params_mutators.add(self._function_key())
        self.generic_visit(node)


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node,
            (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef, ast.Module),
        ):
            continue
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            ids.add(id(node.body[0].value))
    return ids


def _scan_source(
    source: str,
    *,
    relative_path: str,
    call_names: set[str] | None = None,
    include_raw_status: bool = True,
) -> list[_Site]:
    tree = ast.parse(source, filename=relative_path)
    visitor = _TerminalWriteVisitor(
        relative_path=relative_path,
        source=source,
        call_names=(_TERMINAL_CALL_NAMES if call_names is None else call_names),
        docstrings=_docstring_node_ids(tree),
        include_raw_status=include_raw_status,
    )
    visitor.visit(tree)
    return visitor.sites


def _production_sites(
    *,
    call_names: set[str] | None = None,
    include_raw_status: bool = True,
) -> list[_Site]:
    sites: list[_Site] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        sites.extend(
            _scan_source(
                # rule19: closure fence — terminal-write sites enumerated by AST for the reviewed inventory diff
                path.read_text(encoding="utf-8"),
                relative_path=path.relative_to(ROOT).as_posix(),
                call_names=call_names,
                include_raw_status=include_raw_status,
            )
        )
    return sites


def _production_halt_writes() -> tuple[
    set[tuple[str, str]],
    set[tuple[str, str]],
]:
    writers: set[tuple[str, str]] = set()
    halt_via_generic_params_writer: set[tuple[str, str]] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative_path = path.relative_to(ROOT).as_posix()
        # rule19: closure fence — halt writers enumerated by AST across production
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative_path)
        visitor = _HaltWriteVisitor(relative_path=relative_path)
        visitor.visit(tree)
        generic_halt_writers = visitor.params_mutators & visitor.params_update_callers
        writers.update(visitor.column_writers)
        writers.update(generic_halt_writers)
        halt_via_generic_params_writer.update(generic_halt_writers)
    return writers, halt_via_generic_params_writer


def _declared_counter(
    *inventories: dict[SiteKey, Declaration],
) -> Counter[SiteKey]:
    keys: set[SiteKey] = set()
    declared: Counter[SiteKey] = Counter()
    for inventory in inventories:
        overlap = keys & set(inventory)
        assert not overlap, f"terminal-write inventory entries overlap: {overlap}"
        keys.update(inventory)
        for key, (count, reason) in inventory.items():
            assert count > 0
            assert len(reason.split()) >= 8, f"{key} needs a concrete disposition"
            declared[key] = count
    return declared


def _format_drift(
    sites: list[_Site],
    expected: Counter[SiteKey],
) -> str:
    observed = Counter(site.key for site in sites)
    unexpected = observed - expected
    missing = expected - observed
    locations: dict[SiteKey, list[str]] = {}
    for site in sites:
        locations.setdefault(site.key, []).append(
            f"{site.key[0]}:{site.line}: {site.source_line}"
        )
    unexpected_lines = [
        line for key in sorted(unexpected) for line in locations.get(key, [repr(key)])
    ]
    return (
        "project-run terminal-write inventory drifted; a new writer must enter "
        "the fenced terminal kernel or receive an exact, reviewed disposition "
        "in this file.\n"
        f"unaccounted={dict(unexpected)}\n"
        f"stale={dict(missing)}\n" + "\n".join(unexpected_lines)
    )


def test_all_project_run_terminal_write_sites_are_accounted_for() -> None:
    sites = _production_sites()
    expected = _declared_counter(
        PROJECT_RUN_KERNEL_ENTRIES,
        PROJECT_RUN_TERMINAL_API_ENTRIES,
        PROJECT_RUN_KERNEL_IMPLEMENTATION,
        PROJECT_RUN_TERMINAL_SURFACES,
        EXPLAINED_NON_KERNEL_SITES,
    )
    assert Counter(site.key for site in sites) == expected, _format_drift(
        sites, expected
    )


def test_kernel_run_write_retains_source_status_and_owner_guard() -> None:
    path = SOURCE_ROOT / "engine" / "executor" / "project_run_terminalization.py"
    # rule19: closure fence — parses the AST to pin the kernel's guarded SQL
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    writes = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _RAW_RUN_STATUS_UPDATE.search(node.value)
    ]
    assert writes == [
        "UPDATE runs SET status=?, finished_at=datetime('now') "
        "WHERE id=? AND status=? "
        "AND (? IS NULL OR current_attempt_id=?)"
    ]


def test_worker_pre_dispatch_refusals_name_never_dispatched_authority() -> None:
    path = SOURCE_ROOT / "engine" / "jobs" / "runs.py"
    # rule19: closure fence — enumerates terminal-write call sites by AST
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def terminal_calls(node: ast.AST) -> list[ast.Call]:
        return [
            item
            for item in ast.walk(node)
            if isinstance(item, ast.Call)
            and _call_name(item.func)
            in {
                "queued_v1_terminal_failure_result",
                "queued_v1_terminal_receipt_result",
            }
        ]

    def has_never_dispatched_flag(call: ast.Call) -> bool:
        return any(
            keyword.arg == "never_dispatched"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in call.keywords
        )

    pre_run_branch = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "pre_run_error"
        and any(isinstance(operator, ast.IsNot) for operator in node.test.ops)
    )
    route_refusal = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name)
        and node.type.id == "ExecutionRouteVerificationFailed"
    )

    for branch in (pre_run_branch, route_refusal):
        calls = terminal_calls(branch)
        assert len(calls) == 1
        assert has_never_dispatched_flag(calls[0]), (
            "a worker refusal known to precede the dispatch CAS lost its "
            "explicit never-dispatched terminal authority"
        )


def test_runless_receipt_terminalizer_has_its_own_closed_inventory() -> None:
    sites = _production_sites(
        call_names={"_terminalize_action_job_receipt"},
        include_raw_status=False,
    )
    expected = _declared_counter(RUNLESS_RECEIPT_TERMINAL_ENTRIES)
    assert Counter(site.key for site in sites) == expected, _format_drift(
        sites, expected
    )

    runless_function = (
        "src/frisket/engine/executor/action_jobs.py",
        "_terminalize_action_job_receipt",
    )
    project_run_writes = [
        site for site in _production_sites() if site.key[:2] == runless_function
    ]
    assert not project_run_writes, (
        "the runless action-job terminalizer acquired a project-run write; it "
        "must not be folded into the fenced run/attempt kernel"
    )


def test_closure_detects_a_synthetic_new_finish_run_site() -> None:
    source = """
def surprise_terminalizer(project):
    RunResultStore(project).finish_run(41, "cancelled")
"""
    assert [
        site.key
        for site in _scan_source(
            source,
            relative_path="src/frisket/synthetic_terminalizer.py",
        )
    ] == [
        (
            "src/frisket/synthetic_terminalizer.py",
            "surprise_terminalizer",
            "finish_run",
        )
    ]


def test_closure_detects_a_synthetic_new_terminalize_project_run_site() -> None:
    source = """
def surprise_terminalizer(project):
    return terminalize_project_run(
        project,
        run_id=41,
        receipt_id="receipt-surprise",
        status="cancelled",
        authority=object(),
    )
"""
    relative_path = "src/frisket/synthetic_terminalizer.py"
    synthetic_sites = _scan_source(source, relative_path=relative_path)
    key = (
        relative_path,
        "surprise_terminalizer",
        "terminalize_project_run",
    )
    assert [site.key for site in synthetic_sites] == [key]

    expected = _declared_counter(
        PROJECT_RUN_KERNEL_ENTRIES,
        PROJECT_RUN_TERMINAL_API_ENTRIES,
        PROJECT_RUN_KERNEL_IMPLEMENTATION,
        PROJECT_RUN_TERMINAL_SURFACES,
        EXPLAINED_NON_KERNEL_SITES,
    )
    all_sites = [*_production_sites(), *synthetic_sites]
    observed = Counter(site.key for site in all_sites)
    assert observed - expected == Counter({key: 1})
    assert f"{relative_path}:3" in _format_drift(all_sites, expected)


def test_closure_normalizes_a_terminal_kernel_import_alias() -> None:
    source = """
from frisket.engine.executor.project_run_terminalization import (
    terminalize_project_run as close_run,
)

def surprise_terminalizer(project):
    return close_run(
        project,
        run_id=41,
        receipt_id="receipt-surprise",
        status="cancelled",
        authority=object(),
    )
"""
    relative_path = "src/frisket/synthetic_aliased_terminalizer.py"
    assert [
        site.key
        for site in _scan_source(
            source,
            relative_path=relative_path,
        )
    ] == [
        (
            relative_path,
            "surprise_terminalizer",
            "terminalize_project_run",
        )
    ]


def test_closure_detects_a_new_queued_terminal_wrapper_call() -> None:
    source = """
def surprise_terminalizer(project):
    return queued_v1_terminal_receipt_result(
        project,
        project_id="project-surprise",
        run_id=41,
        status="cancelled",
    )
"""
    relative_path = "src/frisket/synthetic_queued_terminalizer.py"
    assert [site.key for site in _scan_source(source, relative_path=relative_path)] == [
        (
            relative_path,
            "surprise_terminalizer",
            "queued_v1_terminal_receipt_result",
        )
    ]


def test_closure_detects_raw_status_sql_but_not_quoted_prose() -> None:
    source = '''
def surprise_terminalizer(db):
    """The old code used UPDATE runs SET status='cancelled'."""
    db.execute("UPDATE runs SET status='cancelled' WHERE id=?", (41,))
'''
    assert [
        site.key
        for site in _scan_source(
            source,
            relative_path="src/frisket/synthetic_terminalizer.py",
        )
    ] == [
        (
            "src/frisket/synthetic_terminalizer.py",
            "surprise_terminalizer",
            "raw_runs_status_sql",
        )
    ]


def test_all_run_halt_writers_are_closed_over() -> None:
    writers, halt_via_generic_params_writer = _production_halt_writes()
    assert writers == {
        (
            "src/frisket/engine/store/runs.py",
            "RunResultStore.set_run_halt",
        ),
        (
            "src/frisket/engine/store/runs.py",
            "RunResultStore.begin_run_resume",
        ),
        (
            "src/frisket/engine/store/runs.py",
            "RunResultStore.revert_run_resume",
        ),
    }
    assert not halt_via_generic_params_writer, (
        "halt keys must not escape the closed writers through update_run_params"
    )


def test_halt_closure_detects_a_synthetic_generic_params_writer() -> None:
    source = """
def surprise_halt_writer(store, run_id, params):
    params["halted_code"] = "surprise"
    store.update_run_params(run_id, params)
"""
    tree = ast.parse(source, filename="src/frisket/synthetic_halt_writer.py")
    visitor = _HaltWriteVisitor(relative_path="src/frisket/synthetic_halt_writer.py")
    visitor.visit(tree)
    assert visitor.params_mutators & visitor.params_update_callers == {
        (
            "src/frisket/synthetic_halt_writer.py",
            "surprise_halt_writer",
        )
    }
