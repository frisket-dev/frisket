"""The deliberately small, scope-enforcing read surface for Project Ask."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from html import unescape
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.features.watchlists.specs import canonical_json
from frisket.server.services.project_qa_query import evaluate_query
from frisket.engine.store.evidence import list_cell_evidence, resolve_evidence_viewer
from frisket.actions.system import root_action_catalog
from frisket.search import search_cells_scoped
from frisket.authoring.action_proposals import (
    proposal_action_ids,
    validate_action_proposals,
)
from frisket.server.services.project_qa_web import safe_web_text, safe_web_url
from frisket.server.services.project_qa_sources import (
    read_source_text,
    find_source_text,
)


MAX_READ_ROWS = 50
MAX_VALUE_CHARS = 2_000
MAX_OBSERVATION_CHARS = 20_000
MAX_SOURCE_CHARS = 8_000


class ProjectQAScopeError(ValueError):
    """A requested project read falls outside the turn's frozen scope."""


def validate_scope(project: Project, scope: Mapping[str, Any]) -> None:
    """Refuse source references that are not visible project metadata.

    This is intentionally a metadata-only admission check.  The actual cell
    read boundary remains ``ProjectQATools``, which also enforces the frozen
    turn scope on every individual tool call.
    """

    if scope.get("kind") == "project":
        return
    if scope.get("kind") != "sources" or not isinstance(scope.get("sources"), list):
        raise ProjectQAScopeError("scope is not a supported source selection")
    visible_sheets = {int(sheet["id"]) for sheet in project.sheets()}
    for source in scope["sources"]:
        if not isinstance(source, Mapping):
            raise ProjectQAScopeError("scope source is invalid")
        sheet_id = source.get("sheet_id")
        if (
            isinstance(sheet_id, bool)
            or not isinstance(sheet_id, int)
            or sheet_id not in visible_sheets
        ):
            raise ProjectQAScopeError("scope references a hidden or missing sheet")
        source_kind = source.get("kind")
        if source_kind == "sheet":
            continue
        if source_kind == "rows":
            row_ids = source.get("row_ids")
            if not isinstance(row_ids, list) or not all(
                isinstance(row_id, int) and not isinstance(row_id, bool)
                for row_id in row_ids
            ):
                raise ProjectQAScopeError("scope references a hidden or missing row")
            if set(project.visible_row_ids(sheet_id, row_ids)) != set(row_ids):
                raise ProjectQAScopeError("scope references a hidden or missing row")
        elif source_kind == "file":
            row_id, column_id = source.get("row_id"), source.get("column_id")
            visible_columns = {
                int(column["id"]) for column in project.columns(sheet_id)
            }
            if (
                isinstance(row_id, bool)
                or not isinstance(row_id, int)
                or isinstance(column_id, bool)
                or not isinstance(column_id, int)
                or column_id not in visible_columns
            ):
                raise ProjectQAScopeError("scope references a hidden or missing cell")
            if project.visible_row_ids(sheet_id, [row_id]) != [row_id]:
                raise ProjectQAScopeError("scope references a hidden or missing cell")
        else:
            raise ProjectQAScopeError("scope source kind is unsupported")


class ProjectQATools:
    """Read project cells only through the immutable submitted turn scope."""

    def __init__(
        self, project: Project, turn: Mapping[str, Any], store: ProjectQAStore
    ) -> None:
        self.project = project
        self.turn = dict(turn)
        self.store = store
        self.turn_id = str(turn["id"])
        self._citation_ids: set[str] = set()
        self._source_handles: dict[str, dict[str, Any]] = {}
        self._source_chars = 0
        self.cancel_event = threading.Event()
        validate_scope(project, self.turn["scope"])

    @property
    def citation_ids(self) -> frozenset[str]:
        return frozenset(self._citation_ids)

    def inspect_sheets(self) -> dict[str, Any]:
        """Return schema/count metadata for sheets the turn may inspect."""

        allowed = self._allowed_sheets()
        out = []
        for sheet in self.project.sheets():
            sheet_id = int(sheet["id"])
            if allowed is not None and sheet_id not in allowed:
                continue
            allowed_rows, file_cells = self._sheet_access(sheet_id)
            columns = self._visible_columns(sheet_id, allowed_rows, file_cells)
            entry = {
                "sheet_id": sheet_id,
                "name": str(sheet["name"]),
                "row_count": self._visible_count(sheet_id),
                "columns": [
                    {
                        "column_id": int(column["id"]),
                        "name": str(column["name"]),
                        "type": str(column["type"]),
                    }
                    for column in columns
                ],
            }
            out.append(entry)
        return {"sheets": out}

    def read_rows(
        self,
        sheet_id: int,
        row_ids: list[int] | None = None,
        column_ids: list[int] | None = None,
        limit: int = MAX_READ_ROWS,
    ) -> dict[str, Any]:
        """Read a bounded, scope-authorized cell set and mint citations."""

        if isinstance(sheet_id, bool) or not isinstance(sheet_id, int):
            raise ProjectQAScopeError("sheet_id must be an integer")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_READ_ROWS
        ):
            raise ValueError(f"limit must be between 1 and {MAX_READ_ROWS}")
        allowed_rows, file_cells = self._sheet_access(sheet_id)
        selected_rows = self._rows_for_read(
            sheet_id, row_ids, allowed_rows, file_cells, limit
        )
        requested_columns = self._requested_columns(sheet_id, column_ids)

        column_by_id = {
            int(column["id"]): column for column in self.project.columns(sheet_id)
        }
        rows: list[dict[str, Any]] = []
        remaining = MAX_OBSERVATION_CHARS
        observation_truncated = False
        for row_id in selected_rows:
            selected_columns = self._columns_for_row(
                requested_columns,
                row_id,
                allowed_rows,
                file_cells,
                explicit_columns=column_ids is not None,
            )
            cells: list[dict[str, Any]] = []
            for column_id in selected_columns:
                if remaining <= 0:
                    observation_truncated = True
                    break
                value, refs = self.project.get_values_with_refs(
                    sheet_id, column_id, row_ids=[row_id], include_validity=True
                )
                if row_id not in value:
                    continue
                cell_value = value[row_id]
                rendered = json.dumps(cell_value, ensure_ascii=False, default=str)
                allowed_chars = min(MAX_VALUE_CHARS, remaining)
                truncated = len(rendered) > allowed_chars
                observation_truncated = observation_truncated or truncated
                excerpt = rendered[:allowed_chars] + ("…" if truncated else "")
                remaining -= len(excerpt)
                column = column_by_id[column_id]
                citation = self.store.add_citation(
                    self.turn_id,
                    label=f"{self._sheet_name(sheet_id)} · row {row_id} · {column['name']}",
                    source_kind="cell",
                    locator={
                        "sheet_id": sheet_id,
                        "row_id": row_id,
                        "column_id": column_id,
                        "value_ref": refs.get(row_id),
                    },
                    excerpt=excerpt,
                    metadata={"truncated": truncated},
                )
                self._citation_ids.add(citation["id"])
                cells.append(
                    {
                        "column_id": column_id,
                        "name": str(column["name"]),
                        "value": cell_value if not truncated else excerpt,
                        "citation_id": citation["id"],
                        "truncated": truncated,
                    }
                )
            rows.append({"row_id": row_id, "cells": cells})
            if observation_truncated:
                break
        return {
            "sheet_id": sheet_id,
            "rows": rows,
            "observation_truncated": observation_truncated,
        }

    def query_rows(
        self,
        query: dict[str, Any],
        limit: int = 50,
        offset: int = 0,
        count_by: int | None = None,
    ) -> dict[str, Any]:
        """Run a canonical sheet filter within the frozen source scope."""
        if not isinstance(query, dict):
            raise ProjectQAScopeError("query must be a frisket.query.v1 object")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 0 <= limit <= MAX_READ_ROWS
        ):
            raise ValueError(f"limit must be between 0 and {MAX_READ_ROWS}")
        try:
            evaluated = evaluate_query(
                self.project,
                query,
                self._query_scope(query),
                limit=limit,
                offset=offset,
                count_by=count_by,
            )
        except ValueError as exc:
            raise ProjectQAScopeError(f"invalid canonical filter: {exc}") from exc
        receipt = {
            "sheet_id": evaluated["sheet_id"],
            "query": evaluated["query"],
            "scope": evaluated["scope"],
            "source_op_cursor": self.project.op_cursor,
            "total": evaluated["total"],
        }
        receipt["query_hash"] = hashlib.sha256(
            canonical_json(receipt).encode()
        ).hexdigest()
        citation = self.store.add_citation(
            self.turn_id,
            label=f"Query on {self._sheet_name(evaluated['sheet_id'])}",
            source_kind="query",
            locator=receipt,
            metadata={"complete": True},
        )
        self._citation_ids.add(citation["id"])
        result: dict[str, Any] = {
            **receipt,
            "row_ids": evaluated["row_ids"],
            "citation_id": citation["id"],
            "complete": True,
        }
        if "count_by" in evaluated:
            result["count_by"] = evaluated["count_by"]
        self.store.append_event(
            self.turn_id,
            kind="result_suggestion",
            payload={
                "citation_id": citation["id"],
                "title": citation["label"],
                "total": evaluated["total"],
            },
        )
        return result

    def search_cells(
        self, query: str, sheet_id: int, limit: int = 20
    ) -> dict[str, Any]:
        """Search scoped cells lexically; results are ranked examples, never counts."""
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError("query must be a non-empty string up to 500 characters")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_READ_ROWS
        ):
            raise ValueError(f"limit must be between 1 and {MAX_READ_ROWS}")
        allowed_rows, file_cells = self._sheet_access(sheet_id)
        hits = search_cells_scoped(
            self.project,
            sheet_id,
            query,
            None if allowed_rows is None else sorted(allowed_rows),
            file_cells,
            limit,
        )
        out = []
        for hit in hits:
            row_id, column_id = int(hit["row_id"]), int(hit["column_id"])
            if (
                allowed_rows is not None
                and row_id not in allowed_rows
                and (row_id, column_id) not in file_cells
            ):
                continue
            source = read_source_text(
                self.project,
                (sheet_id, row_id, column_id),
                limit=1,
                cancel=self.cancel_event,
            )
            refs = {row_id: source["value_ref"]}
            citation = self.store.add_citation(
                self.turn_id,
                label=(
                    f"Search hit · {self._sheet_name(sheet_id)} · row {row_id} "
                    f"· {hit['column_name']}"
                ),
                source_kind="cell",
                locator={
                    "sheet_id": sheet_id,
                    "row_id": row_id,
                    "column_id": column_id,
                    "value_ref": refs.get(row_id),
                },
                excerpt=str(hit["snip"]),
                metadata={"search": query},
            )
            self._citation_ids.add(citation["id"])
            out.append(
                {
                    "sheet_id": sheet_id,
                    "row_id": row_id,
                    "column_id": column_id,
                    "snippet": hit["snip"],
                    "citation_id": citation["id"],
                }
            )
        return {"query": query, "hits": out, "partial": True}

    def record_web_search(self, search: Mapping[str, Any]) -> dict[str, Any]:
        """Persist safe public-search snippets as answer-citable receipts."""

        retrieved_at = search.get("retrieved_at")
        results = search.get("results")
        if not isinstance(retrieved_at, str) or not isinstance(results, list):
            raise ValueError("web search result is malformed")
        saved: list[dict[str, Any]] = []
        for result in results:
            if not isinstance(result, Mapping):
                continue
            url = safe_web_url(result.get("url"))
            if url is None:
                continue
            title = safe_web_text(result.get("title"), limit=160) or "Web result"
            snippet = safe_web_text(result.get("snippet"), limit=600)
            citation = self._record_web_citation(
                label=title,
                url=url,
                excerpt=snippet or None,
                retrieved_at=retrieved_at,
                fetched=False,
            )
            saved.append(
                {
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "citation_id": citation["id"],
                }
            )
        return {
            "query": search.get("query", ""),
            "results": saved,
            "retrieved_at": retrieved_at,
        }

    def record_web_page(self, page: Mapping[str, Any]) -> dict[str, Any]:
        """Persist one bounded fetched public page as a citable receipt."""

        url = safe_web_url(page.get("url"))
        retrieved_at = page.get("retrieved_at")
        if url is None or not isinstance(retrieved_at, str):
            raise ValueError("web page result is malformed")
        text = safe_web_text(page.get("text"), limit=MAX_SOURCE_CHARS)
        citation = self._record_web_citation(
            label=urlsplit(url).hostname or "Web page",
            url=url,
            excerpt=text or None,
            retrieved_at=retrieved_at,
            fetched=True,
        )
        return {
            "url": url,
            "text": text,
            "citation_id": citation["id"],
            "truncated": bool(page.get("truncated")),
            "retrieved_at": retrieved_at,
        }

    def _record_web_citation(
        self,
        *,
        label: str,
        url: str,
        excerpt: str | None,
        retrieved_at: str,
        fetched: bool,
    ) -> dict[str, Any]:
        citation = self.store.add_citation(
            self.turn_id,
            label=label,
            source_kind="web",
            locator={"url": url, "retrieved_at": retrieved_at, "fetched": fetched},
            excerpt=excerpt,
            metadata={"fetched": fetched},
        )
        self._citation_ids.add(citation["id"])
        return citation

    def _source_citation(self, citation_id: str) -> dict[str, Any]:
        citation = self.store.get_citation(citation_id)
        if (
            self.store.get_turn(citation["turn_id"])["thread_id"]
            != self.turn["thread_id"]
        ):
            raise ProjectQAScopeError("source belongs to another conversation")
        locator = citation["locator"]
        if citation["source_kind"] == "query":
            self._query_scope(locator["query"])
        elif citation["source_kind"] in {"cell", "evidence"}:
            sheet_id, row_id, column_id = (
                int(locator[key]) for key in ("sheet_id", "row_id", "column_id")
            )
            allowed_rows, file_cells = self._sheet_access(sheet_id)
            self._requested_columns(sheet_id, [column_id])
            if not self._rows_for_read(sheet_id, [row_id], allowed_rows, file_cells, 1):
                raise ProjectQAScopeError("source row is hidden or no longer available")
            self._columns_for_row(
                [column_id], row_id, allowed_rows, file_cells, explicit_columns=True
            )
        elif citation["source_kind"] != "web":
            raise ProjectQAScopeError("source kind cannot be opened")
        return citation

    def _source_handle(
        self,
        *,
        label: str,
        source_kind: str,
        locator: dict[str, Any],
        excerpt: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = canonical_json({"kind": source_kind, "locator": locator})
        citation = self._source_handles.get(key)
        if citation is None:
            citation = self.store.add_citation(
                self.turn_id,
                label=label,
                source_kind=source_kind,
                locator=locator,
                excerpt=excerpt,
                metadata=metadata or {},
            )
            self._source_handles[key] = citation
        self._citation_ids.add(citation["id"])
        return citation

    def list_sources(self, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """Discover prior source handles without returning inaccessible labels."""
        if not 1 <= limit <= 50 or offset < 0:
            raise ValueError("source page requires limit 1–50 and a nonnegative offset")
        candidates = self.project.db.execute(
            "SELECT c.id FROM project_qa_citations c JOIN project_qa_turns t ON t.id=c.turn_id "
            "WHERE t.thread_id=? ORDER BY c.created_at DESC,c.id DESC LIMIT ? OFFSET ?",
            (self.turn["thread_id"], limit + 1, offset),
        ).fetchall()
        sources = []
        for row in candidates[:limit]:
            try:
                citation = self._source_citation(row["id"])
            except (ValueError, LookupError):
                continue
            sources.append(
                {
                    "citation_id": citation["id"],
                    "label": citation["label"],
                    "source_kind": citation["source_kind"],
                }
            )
        return {
            "sources": sources,
            "has_more": len(candidates) > limit,
            "next_offset": offset + limit if len(candidates) > limit else None,
        }

    def open_source(
        self, citation_id: str, cursor: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Read near a hit or continue it, including prior same-thread sources."""
        citation = self._source_citation(citation_id)
        locator = citation["locator"]
        if citation["source_kind"] == "query":
            result = self.query_rows(locator["query"])
            result["scope_changed"] = locator.get("scope") != result["scope"]
            return result
        remaining = 64_000 - self._source_chars
        if remaining <= 0:
            return {
                "available": True,
                "passages": [],
                "truncated": True,
                "coverage": "source reading budget exhausted",
            }
        if citation["source_kind"] == "web":
            text = str(citation["excerpt"] or "")[: min(MAX_SOURCE_CHARS, remaining)]
            current = self._source_handle(
                label=citation["label"],
                source_kind="web",
                locator=locator,
                excerpt=text,
                metadata={"previously_retrieved": True},
            )
            self._source_chars += len(text)
            return {
                "citation_id": current["id"],
                "available": True,
                "excerpt": text,
                "previously_retrieved": True,
                "passages": [{"kind": "web", "text": text}],
            }
        cell = tuple(int(locator[key]) for key in ("sheet_id", "row_id", "column_id"))
        marked = re.search(r"<b>(.*?)</b>", str(citation["excerpt"] or ""), re.DOTALL)
        anchor = unescape(marked.group(1)) if marked else None
        start = int(locator.get("char_start", 0))
        # Reserve room for grounded page/time evidence instead of always exhausting
        # the complete read allowance on a cell prefix.
        observed = read_source_text(
            self.project,
            cell,
            cursor=cursor,
            start=start,
            anchor=anchor,
            limit=min(6_000, remaining),
            cancel=self.cancel_event,
        )
        text = observed["text"]
        current_locator = {
            **locator,
            "value_ref": observed["value_ref"],
            "char_start": observed["range"]["start"],
            "char_end": observed["range"]["end"],
            "source_version": observed["version"],
        }
        current = self._source_handle(
            label=citation["label"],
            source_kind="cell",
            locator=current_locator,
            excerpt=text,
        )
        passages = [{"kind": "cell", "text": text, "citation_id": current["id"]}]
        room = min(MAX_SOURCE_CHARS, remaining) - len(text)
        has_evidence = self.project.db.execute(
            "SELECT 1 FROM evidence_links WHERE sheet_id=? AND row_id=? "
            "AND column_id=? AND status='active' LIMIT 1",
            cell,
        ).fetchone()
        evidence = (
            list_cell_evidence(
                self.project, sheet_id=cell[0], row_id=cell[1], column_id=cell[2]
            )
            if has_evidence
            else {"links": []}
        )
        for link in evidence["links"]:
            if room <= 0:
                break
            viewer = resolve_evidence_viewer(self.project, link["stable_id"])
            if (
                viewer["link"]["status"] != "active"
                or viewer["link"]["text_layer_hash_mismatch"]
            ):
                continue
            for artifact in viewer.get("artifacts", []):
                for span in artifact.get("spans", []):
                    quote = span.get("quote") or span.get("snippet")
                    if not isinstance(quote, str) or not quote or room <= 0:
                        continue
                    if observed["length"] > MAX_SOURCE_CHARS and quote not in text:
                        continue
                    clipped = quote[:room]
                    evidence_locator = {
                        "sheet_id": cell[0],
                        "row_id": cell[1],
                        "column_id": cell[2],
                        "value_ref": observed["value_ref"],
                        "evidence_link_id": link["stable_id"],
                        "artifact_id": artifact["stable_id"],
                        "span_id": span["stable_id"],
                    }
                    prepared = self._source_handle(
                        label=f"Prepared evidence · {artifact.get('title') or artifact.get('filename') or citation['label']}",
                        source_kind="evidence",
                        locator=evidence_locator,
                        excerpt=clipped,
                    )
                    passages.append(
                        {
                            "kind": "prepared_evidence",
                            "citation_id": prepared["id"],
                            "text": clipped,
                            **evidence_locator,
                            **{
                                key: span.get(key)
                                for key in (
                                    "page_start",
                                    "page_end",
                                    "start_ms",
                                    "end_ms",
                                    "bbox",
                                )
                            },
                        }
                    )
                    room -= len(clipped)
        self._source_chars += sum(len(part["text"]) for part in passages)
        return {
            "citation_id": current["id"],
            "available": True,
            "passages": passages,
            "range": observed["range"],
            "next_cursor": observed["next_cursor"],
            "truncated": not observed["reached_end"],
            "reached_end": observed["reached_end"],
            "source_changed": locator.get("value_ref") != observed["value_ref"],
            "remaining_read_chars": 64_000 - self._source_chars,
        }

    def find_in_source(
        self, citation_id: str, literal: str, cursor: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        citation = self._source_citation(citation_id)
        if citation["source_kind"] not in {"cell", "evidence"}:
            raise ValueError("literal find requires a project text source")
        if self._source_chars >= 64_000:
            return {
                "matches": [],
                "reached_end": False,
                "coverage": "source reading budget exhausted",
            }
        locator = citation["locator"]
        cell = tuple(int(locator[key]) for key in ("sheet_id", "row_id", "column_id"))
        result = find_source_text(
            self.project, cell, literal, cursor=cursor, cancel=self.cancel_event
        )
        matches = []
        for match in result["matches"]:
            remaining = 64_000 - self._source_chars
            if remaining <= 0:
                result["reached_end"] = False
                break
            match = {**match, "text": match["text"][:remaining]}
            current = self._source_handle(
                label=citation["label"],
                source_kind="cell",
                locator={
                    **locator,
                    "value_ref": result["value_ref"],
                    "source_version": result["version"],
                    "char_start": match["start"],
                    "char_end": match["end"],
                },
                excerpt=match["text"],
            )
            matches.append({**match, "citation_id": current["id"]})
            self._source_chars += len(match["text"])
        return {
            "matches": matches,
            "scanned_range": result["scanned_range"],
            "reached_end": result["reached_end"],
            "next_cursor": result["next_cursor"],
        }

    def search_actions(self, query: str, limit: int = 8) -> dict[str, Any]:
        """Discover compact action descriptions; load a chosen schema separately."""
        if not self.turn["suggest_actions"]:
            raise ProjectQAScopeError("action suggestions are disabled for this turn")
        if not query.strip() or len(query) > 500 or not 1 <= limit <= 20:
            raise ValueError("use a short action query and limit 1–20")
        terms = query.casefold().split()
        allowed = proposal_action_ids()
        ranked = []
        for entry in root_action_catalog().actions:
            if entry.kind not in allowed:
                continue
            text = f"{entry.kind} {entry.title} {entry.description}".casefold()
            score = sum(term in text for term in terms)
            if score:
                ranked.append((score, entry))
        ranked.sort(key=lambda item: (-item[0], item[1].kind))
        return {
            "actions": [
                {
                    "action_id": entry.kind,
                    "title": entry.title,
                    "description": entry.description,
                }
                for _, entry in ranked[:limit]
            ],
            "has_more": len(ranked) > limit,
        }

    def propose_action(
        self, kind: str, title: str, spec: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate a canonical proposal only; Ask has no execution authority."""
        if not self.turn["suggest_actions"]:
            raise ProjectQAScopeError("action suggestions are disabled for this turn")
        proposals = validate_action_proposals(
            self.project,
            [{"kind": kind, "title": title, "spec": spec}],
            scope=self.turn["scope"],
        )
        if not proposals:
            raise ProjectQAScopeError(
                "proposal is not an available action for this project"
            )
        proposal = proposals[0]
        self.store.append_event(
            self.turn_id, kind="action_proposal", payload={"proposal": proposal}
        )
        return {"proposal": proposal}

    def describe_action(self, action_id: str) -> dict[str, Any]:
        """Return one registered action's compact parameter schema for a draft."""

        if not self.turn["suggest_actions"]:
            raise ProjectQAScopeError("action suggestions are disabled for this turn")
        if not isinstance(action_id, str) or not action_id:
            raise ValueError("action_id must be a non-empty string")
        if action_id not in proposal_action_ids():
            raise ProjectQAScopeError("action is not available")
        try:
            entry = next(
                item for item in root_action_catalog().actions if item.kind == action_id
            )
        except StopIteration as exc:
            raise ProjectQAScopeError("action is not available") from exc
        return {
            "action_id": entry.kind,
            "title": entry.title,
            "description": entry.description,
            "input_schema": entry.input_schema,
        }

    def _query_scope(self, query: Mapping[str, Any]) -> dict[str, Any]:
        """Translate frozen sources into a replayable query constraint."""

        scope = query.get("scope")
        if not isinstance(scope, Mapping) or not isinstance(scope.get("sheet_id"), int):
            raise ProjectQAScopeError("query scope must name a sheet")
        sheet_id = int(scope["sheet_id"])
        allowed_rows, file_cells = self._sheet_access(sheet_id)
        if allowed_rows is None:
            return {"kind": "sheet", "sheet_id": sheet_id}
        file_rows = {row_id for row_id, _column_id in file_cells}
        if file_cells and allowed_rows and not file_rows <= allowed_rows:
            columns = {column_id for _row_id, column_id in file_cells}
            if file_cells != {
                (row_id, column_id) for row_id in file_rows for column_id in columns
            }:
                raise ProjectQAScopeError(
                    "query cannot combine different file cells; read or search them instead"
                )
            raise ProjectQAScopeError(
                "query cannot combine file cells with separate row selections"
            )
        rows = allowed_rows | {row_id for row_id, _column_id in file_cells}
        if not rows:
            raise ProjectQAScopeError("query scope has no readable rows")
        result: dict[str, Any] = {
            "kind": "rows",
            "sheet_id": sheet_id,
            "row_ids": sorted(rows),
        }
        if file_cells and not allowed_rows:
            columns = {column_id for _row_id, column_id in file_cells}
            if file_cells != {
                (row_id, column_id) for row_id in rows for column_id in columns
            }:
                raise ProjectQAScopeError(
                    "query requires the same selected file column for every selected row"
                )
            result["column_ids"] = sorted(columns)
        return result

    def _allowed_sheets(self) -> set[int] | None:
        scope = self.turn["scope"]
        if scope["kind"] == "project":
            return None
        return {int(source["sheet_id"]) for source in scope["sources"]}

    def _sheet_access(
        self, sheet_id: int
    ) -> tuple[set[int] | None, set[tuple[int, int]]]:
        if sheet_id not in {int(sheet["id"]) for sheet in self.project.sheets()}:
            raise ProjectQAScopeError("sheet is outside this turn's scope")
        if (
            self._allowed_sheets() is not None
            and sheet_id not in self._allowed_sheets()
        ):
            raise ProjectQAScopeError("sheet is outside this turn's scope")
        scope = self.turn["scope"]
        if scope["kind"] == "project":
            return None, set()
        rows: set[int] = set()
        whole_sheet = False
        file_cells: set[tuple[int, int]] = set()
        for source in scope["sources"]:
            if int(source["sheet_id"]) != sheet_id:
                continue
            if source["kind"] == "sheet":
                whole_sheet = True
            elif source["kind"] == "rows":
                rows.update(int(row_id) for row_id in source["row_ids"])
            elif source["kind"] == "file":
                file_cells.add((int(source["row_id"]), int(source["column_id"])))
        return (None if whole_sheet else rows), file_cells

    def _rows_for_read(
        self,
        sheet_id: int,
        requested: list[int] | None,
        allowed_rows: set[int] | None,
        file_cells: set[tuple[int, int]],
        limit: int,
    ) -> list[int]:
        if requested is not None:
            if not all(
                isinstance(row_id, int) and not isinstance(row_id, bool)
                for row_id in requested
            ):
                raise ProjectQAScopeError("row_ids must contain integers")
            candidate = list(dict.fromkeys(requested))
            permitted_rows = (allowed_rows or set()) | {
                row_id for row_id, _column_id in file_cells
            }
            if allowed_rows is not None and not set(candidate) <= permitted_rows:
                raise ProjectQAScopeError("row is outside this turn's scope")
            if len(candidate) > limit:
                raise ValueError(f"read may include at most {limit} rows")
        elif allowed_rows is not None:
            candidate = sorted(
                allowed_rows | {row_id for row_id, _column_id in file_cells}
            )[:limit]
        else:
            candidate = [
                int(row["id"])
                for row in self.project.db.execute(
                    "SELECT id FROM rows WHERE sheet_id=? AND hidden=0 ORDER BY position LIMIT ?",
                    (sheet_id, limit),
                )
            ]
        return self.project.visible_row_ids(sheet_id, candidate)

    def _requested_columns(
        self,
        sheet_id: int,
        requested: list[int] | None,
    ) -> list[int]:
        available = {int(column["id"]) for column in self.project.columns(sheet_id)}
        if requested is None:
            selected = sorted(available)
        else:
            if not all(
                isinstance(column_id, int) and not isinstance(column_id, bool)
                for column_id in requested
            ):
                raise ProjectQAScopeError("column_ids must contain integers")
            selected = list(dict.fromkeys(requested))
        if not set(selected) <= available:
            raise ProjectQAScopeError("column is not in sheet")
        return selected

    def _visible_columns(
        self,
        sheet_id: int,
        allowed_rows: set[int] | None,
        file_cells: set[tuple[int, int]],
    ) -> list[Any]:
        columns = self.project.columns(sheet_id)
        if allowed_rows is None or allowed_rows:
            return columns
        allowed_column_ids = {column_id for _row_id, column_id in file_cells}
        return [column for column in columns if int(column["id"]) in allowed_column_ids]

    @staticmethod
    def _columns_for_row(
        requested_columns: list[int],
        row_id: int,
        allowed_rows: set[int] | None,
        file_cells: set[tuple[int, int]],
        *,
        explicit_columns: bool,
    ) -> list[int]:
        """A file scope can widen only its one selected cell, never its row."""

        if allowed_rows is None or row_id in allowed_rows:
            return requested_columns
        allowed_file_columns = {
            column_id for file_row_id, column_id in file_cells if file_row_id == row_id
        }
        if explicit_columns and not set(requested_columns) <= allowed_file_columns:
            raise ProjectQAScopeError("file scope may read only the selected file cell")
        selected = [
            column_id
            for column_id in requested_columns
            if column_id in allowed_file_columns
        ]
        if not selected:
            raise ProjectQAScopeError("file scope may read only the selected file cell")
        return selected

    def _visible_count(self, sheet_id: int) -> int:
        allowed_rows, file_cells = self._sheet_access(sheet_id)
        if allowed_rows is None:
            return self.project.row_count(sheet_id)
        row_ids = allowed_rows | {row_id for row_id, _column_id in file_cells}
        return len(self.project.visible_row_ids(sheet_id, sorted(row_ids)))

    def _sheet_name(self, sheet_id: int) -> str:
        row = self.project.db.execute(
            "SELECT name FROM sheets WHERE id=?", (sheet_id,)
        ).fetchone()
        return str(row["name"]) if row is not None else f"Sheet {sheet_id}"

    @staticmethod
    def _bounded_text(value: Any, limit: int) -> str:
        text = json.dumps(value, ensure_ascii=False, default=str)
        return text[:limit] + ("…" if len(text) > limit else "")
