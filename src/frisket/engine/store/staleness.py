"""Derived-sheet freshness from actual consumed sources, never op cursors."""

from __future__ import annotations

import json
from typing import Any
from collections.abc import Sequence

from frisket.engine.store.consumed_sources import bundle_state


STALE_REASON_PARENT_CHANGED = "parent_changed"
_PRIORITY = {"fresh": 0, "unknown": 1, "checking": 2, "stale": 3}


def compute_sync_states(
    project: Any, *, verify: bool = True, sheet_ids: Sequence[int] | None = None
) -> dict[int, dict[str, str | None]]:
    """Cheap cached reads; expensive changed captures are requested on demand.

    Old derived outputs without an actual source capture are unknown. Neither
    queueing nor an operation cursor invents the inputs they consumed.
    """
    sheets = project.db.execute(
        "SELECT id,parent_sheet_id FROM sheets WHERE hidden=0"
    ).fetchall()
    parents = {int(row["id"]): row["parent_sheet_id"] for row in sheets}
    derived = {identity for identity, parent in parents.items() if parent is not None}
    if not derived:
        return {}
    request = getattr(project, "_frisket_request_source_verification", None)
    own: dict[int, str] = {}
    sources: dict[int, set[int]] = {}
    requested = (
        derived if sheet_ids is None else derived.intersection(map(int, sheet_ids))
    )
    pending = set(requested)
    while pending:
        sheet_id = pending.pop()
        if sheet_id in own:
            continue
        bundles = project.db.execute(
            "SELECT b.id,b.owner_op_id,b.capture_json,o.kind,o.spec FROM consumed_source_bundles b "
            "JOIN ops o ON o.id=b.owner_op_id "
            "WHERE b.output_sheet_id=? AND b.kind='aggregate' AND b.row_id IS NULL AND o.status='applied' "
            # Row consumers' shared corpora belong to those result rows; they
            # do not replace the derived sheet's materialization capture.
            "AND instr(b.capture_key,':shared:')=0 "
            "AND o.change_order IS NOT NULL "
            "ORDER BY o.change_order DESC,b.id",
            (sheet_id,),
        ).fetchall()
        latest_owner = int(bundles[0]["owner_op_id"]) if bundles else None
        states = []
        owner_spec = json.loads(bundles[0]["spec"]) if bundles else {}
        if bundles and (
            bundles[0]["kind"] == "sheet.refresh" or "refresh_receipt_id" in owner_spec
        ):
            receipt = project.db.execute(
                "SELECT status,body FROM receipts WHERE id=?",
                (owner_spec.get("refresh_receipt_id"),),
            ).fetchone()
            if (
                receipt is None
                or receipt["status"] != "completed"
                or latest_owner not in json.loads(receipt["body"]).get("op_ids", ())
            ):
                # Inputs may have been read completely while only some output
                # batches committed. The exact owning receipt proves coverage.
                states.append("unknown")
        source_ids = {int(parents[sheet_id])}
        for bundle in bundles:
            if int(bundle["owner_op_id"]) != latest_owner:
                break
            states.append(
                bundle_state(
                    project,
                    int(bundle["id"]),
                    request_verification=request,
                    allow_inline=verify,
                )
            )
            capture = json.loads(bundle["capture_json"])
            source_ids.update(
                int(scope["sheet_id"]) for scope in capture.get("scopes", ())
            )
        own[sheet_id] = max(states, key=_PRIORITY.__getitem__) if states else "unknown"
        sources[sheet_id] = source_ids
        pending.update(source_ids.intersection(derived).difference(own))

    resolved: dict[int, str] = {}

    def state(sheet_id: int, visiting: frozenset[int] = frozenset()) -> str:
        if sheet_id in resolved:
            return resolved[sheet_id]
        if sheet_id in visiting:
            return "unknown"
        candidates = [own[sheet_id]]
        for source_id in sources[sheet_id]:
            if source_id in own:
                candidates.append(state(source_id, visiting | {sheet_id}))
        result = max(candidates, key=_PRIORITY.__getitem__)
        resolved[sheet_id] = result
        return result

    return {
        sheet_id: {
            "sync_state": "synced"
            if (result := state(sheet_id)) == "fresh"
            else result,
            "stale_reason": {
                "fresh": None,
                "stale": STALE_REASON_PARENT_CHANGED,
                "unknown": "source_capture_unknown",
                "checking": "checking_source_changes",
            }[result],
        }
        for sheet_id in sorted(requested)
    }


def sheet_freshness(project: Any, sheet_id: int, *, verify: bool = True) -> str:
    sheet = project.db.execute(
        "SELECT parent_sheet_id FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if sheet is None or sheet[0] is None:
        return "synced"
    entry = compute_sync_states(project, verify=verify, sheet_ids=[sheet_id]).get(
        int(sheet_id)
    )
    return str(entry["sync_state"]) if entry is not None else "synced"


def sheet_is_stale(project: Any, sheet_id: int) -> bool:
    """Return the stale warning; callers can separately expose unknown/checking."""
    return sheet_freshness(project, sheet_id) == "stale"
