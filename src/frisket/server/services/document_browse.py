"""Bounded document descriptors and identity-based, bidirectional keysets."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, date
import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

from frisket.querysets import SheetOrderTerm, sheet_row_scope_plan
from frisket.engine.store.text_annotations import annotated_text_column_ids


def _encode(payload: dict) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()


def _decode(cursor: str) -> dict:
    try:
        if len(cursor) > 2048:
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(cursor))
        if not isinstance(value, dict) or set(value) != {
            "scope",
            "row",
            "ordinal",
            "back",
            "date",
        }:
            raise ValueError
        if (
            type(value["row"]) is not int
            or value["row"] <= 0
            or type(value["ordinal"]) is not int
            or value["ordinal"] < 1
            or type(value["back"]) is not bool
        ):
            raise ValueError
        date.fromisoformat(value["date"])
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("invalid document cursor; restart browsing") from exc


def _seek(
    terms: list[SheetOrderTerm], *, back: bool, inclusive: bool = False
) -> tuple[str, list[Any]]:
    # The ordinary order is indexed directly; row-value comparison makes deep
    # default pages a seek instead of a scan from the start of the sheet.
    if len(terms) == 2:
        op = "<" if back else ">"
        return f"(r.position,r.id) {op}{'=' if inclusive else ''} (a.k0,a.k1)", []
    clauses, params = [], []
    for index, term in enumerate(terms):
        equal = []
        for previous, prefix in enumerate(terms[:index]):
            equal.append(f"(({prefix.sql}){prefix.collation} IS a.k{previous})")
            params.extend(prefix.params)
        op = "<" if term.descending != back else ">"
        clauses.append(
            "("
            + " AND ".join([*equal, f"(({term.sql}){term.collation} {op} a.k{index})"])
            + ")"
        )
        params.extend(term.params)
    if inclusive:
        clauses.append("r.id=a.row_id")
    return "(" + " OR ".join(clauses) + ")", params


def _descriptor_sql(
    source: Any, title: Any | None, *, row_id_sql: str = "?", title_only: bool = False
) -> tuple[str, list[Any]]:
    # Values stay inside SQLite. Only capped labels and scalar metadata leave it.
    media = source["type"] in {"file", "image", "audio", "video"}
    source_id = int(source["id"])
    title_id = int(title["id"]) if title is not None else -1
    read_source = media or not title_only
    source_value = (
        "CASE WHEN s.validity='valid' THEN s.value END" if read_source else "NULL"
    )
    source_kind = (
        "CASE WHEN s.validity='valid' THEN s.value_kind END" if read_source else "NULL"
    )
    source_join = (
        "LEFT JOIN current_cell_values s ON s.row_id=r.id AND s.column_id=?"
        if read_source
        else ""
    )
    # Titles are display values, like /data's preserve-invalid projection.
    title_value = "t.value"
    title_kind = "t.value_kind"
    # Media envelopes are the only source values handed to JSON1.
    envelope = (
        "CASE WHEN sk='json' THEN CASE WHEN json_type(sv)='object' THEN sv END END"
        if media
        else "NULL"
    )
    label = (
        "COALESCE(CASE WHEN json_type(envelope,'$.filename')='text' THEN NULLIF(json_extract(envelope,'$.filename'),'') END,CASE WHEN json_type(envelope,'$.blob')='text' THEN substr(json_extract(envelope,'$.blob'),1,12) END,CASE WHEN sk='text' THEN frisket_document_url_label(sv) END)"
        if media
        else "NULL"
    )
    sql = f"""
        SELECT *, CASE WHEN raw_title IS NOT NULL AND trim(CAST(raw_title AS TEXT))<>''
            AND substr(CAST(raw_title AS TEXT),1,1)<>'{{' THEN CAST(raw_title AS TEXT)
            ELSE label END AS display_title
        FROM (
            SELECT *, {label} AS label,
                CASE WHEN tk='boolean' THEN CASE WHEN tv THEN 'true' ELSE 'false' END
                WHEN tk IN ('text','integer','real','bigint','json','legacy_invalid') THEN tv END AS raw_title,
                CASE WHEN json_type(envelope,'$.mime')='text' THEN json_extract(envelope,'$.mime') END AS mime
            FROM (
                SELECT *, {envelope} AS envelope FROM (
                    SELECT r.id AS row_id,r.position,{source_value} AS sv,
                        {source_kind} AS sk,{title_value} AS tv,{title_kind} AS tk
                    FROM rows r {source_join}
                    LEFT JOIN current_cell_values t ON t.row_id=r.id AND t.column_id=?
                    WHERE r.id={row_id_sql}
                )
            )
        )
    """
    return sql, ([source_id] if read_source else []) + [title_id]


def _url_label(value: str) -> str:
    try:
        path = urlsplit(value).path if value.startswith("http") else value
        return path.rstrip("/").rsplit("/", 1)[-1] or value
    except ValueError:
        return value


def document_browse(project, sheet_id: int, **kwargs) -> dict:
    """One page is internally consistent; cursors are live navigation, not exports.

    Existing runtime/plugin filters retain their whole-scope evaluation contract;
    arbitrary filtering/sorting may require SQL work beyond the bounded payload.
    """
    # Keep the full Project read API for runtime filter handlers. This sync
    # request owns one thread-local connection, so no cross-thread facade is
    # needed. Never finish a transaction opened by an enclosing caller.
    connection = project.db
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        return _document_browse(
            project, sheet_id, _storage_identity=project.storage_identity, **kwargs
        )
    finally:
        if owns_transaction:
            connection.rollback()


def _document_browse(
    project,
    sheet_id: int,
    *,
    _storage_identity: str,
    source_column_id: int,
    parent_row_id: int | None = None,
    title_column_id: int | None = None,
    filter: dict | None = None,
    sort: list | None = None,
    scope_row_ids: list[int] | None = None,
    q: str | None = None,
    cursor: str | None = None,
    anchor_row_id: int | None = None,
    limit: int = 100,
) -> dict:
    project.db.create_function(
        "frisket_document_url_label", 1, _url_label, deterministic=True
    )
    project.db.create_function(
        "frisket_document_contains",
        2,
        lambda title, query: query.lower() in (title or "").lower(),
        deterministic=True,
    )
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("document page limit must be between 1 and 200")
    if cursor is not None and anchor_row_id is not None:
        raise ValueError("use cursor or anchor_row_id, not both")
    q = q.strip() if q else None
    sheet = project.db.execute(
        "SELECT title_column_id FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if sheet is None:
        raise ValueError("sheet not found")
    decoded = _decode(cursor) if cursor is not None else None
    today = decoded["date"] if decoded else datetime.now(UTC).date().isoformat()
    filter_raw = json.dumps(filter) if filter is not None else None
    sort_raw = json.dumps(sort) if sort is not None else None
    scope_plan = sheet_row_scope_plan(
        project,
        sheet_id,
        parent_row_id=parent_row_id,
        filter_=filter_raw,
        sort=sort_raw,
        row_ids=scope_row_ids,
        reference_date=date.fromisoformat(today),
    )
    columns = scope_plan.columns
    where = scope_plan.where_sql
    where_params = scope_plan.where_params
    by_id = {int(column["id"]): column for column in columns}
    if (
        source_column_id not in by_id
        or title_column_id is not None
        and title_column_id not in by_id
    ):
        raise ValueError("document source or title column not found")
    if title_column_id is None:
        title_column_id = (
            sheet["title_column_id"] if sheet["title_column_id"] in by_id else None
        ) or (int(columns[0]["id"]) if columns else None)
    source = by_id[source_column_id]
    if source["type"] not in {
        "file",
        "image",
        "audio",
        "video",
    } and source_column_id not in annotated_text_column_ids(project, sheet_id):
        raise ValueError("column is not a document source")
    title = by_id.get(title_column_id)
    descriptor, descriptor_params = _descriptor_sql(source, title)
    terms = list(scope_plan.order_terms)
    order_params = [value for term in terms for value in term.params]
    ctes: list[str] = []
    cte_params: list[Any] = []
    membership_source = scope_plan.filter_from_sql
    membership_params = list(scope_plan.filter_join_params)
    ordered_source = scope_plan.from_sql
    ordered_params = list(scope_plan.join_params)
    # Only explicit title search needs pre-search ranks for the existing Row N
    # fallback. Materialize identities/ranks in SQLite, never source bodies.
    if q:
        normal_order = ",".join(term.order_sql() for term in terms)
        ctes.append(
            f"ranked AS MATERIALIZED (SELECT r.id,row_number() OVER (ORDER BY {normal_order}) AS ordinal FROM {ordered_source} WHERE {where})"
        )
        # Window ORDER BY is lexically before FROM, unlike an ordinary SELECT.
        cte_params.extend([*order_params, *ordered_params, *where_params])
        membership_source = "rows r JOIN ranked z ON z.id=r.id"
        membership_params = []
        ordered_source += " JOIN ranked z ON z.id=r.id"
        title_query, title_params = _descriptor_sql(
            source, title, row_id_sql="outer_row.id", title_only=True
        )
        where = f"EXISTS (SELECT 1 FROM rows outer_row WHERE outer_row.id=r.id AND frisket_document_contains(COALESCE((SELECT display_title FROM ({title_query})),'Row '||z.ordinal),?))"
        where_params = [*title_params, q]
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                _storage_identity,
                sheet_id,
                parent_row_id,
                source_column_id,
                title_column_id,
                filter,
                sort,
                scope_row_ids,
                q,
                today,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if decoded and decoded["scope"] != fingerprint:
        raise ValueError("document cursor scope changed; restart browsing")
    anchor = decoded["row"] if decoded else anchor_row_id
    back = decoded["back"] if decoded else False
    prefix = "WITH " + ",".join(ctes) + " " if ctes else ""
    seek, seek_params = "1=1", []
    ordinal = decoded["ordinal"] if decoded else 1
    has_prior = decoded is not None
    if anchor is not None:
        if (
            project.db.execute(
                prefix + f"SELECT 1 FROM {membership_source} WHERE r.id=? AND {where}",
                [*cte_params, *membership_params, anchor, *where_params],
            ).fetchone()
            is None
        ):
            raise ValueError(
                "document cursor anchor is no longer in scope; restart browsing"
            )
        keys = ",".join(f"{term.sql} AS k{i}" for i, term in enumerate(terms))
        ctes.append(
            f"a AS MATERIALIZED (SELECT r.id AS row_id,{keys} FROM {scope_plan.from_sql} WHERE r.id=?)"
        )
        cte_params.extend([*order_params, *scope_plan.join_params, anchor])
        prefix = "WITH " + ",".join(ctes) + " "
        ordered_source = "a CROSS JOIN " + ordered_source
        seek, seek_params = _seek(terms, back=back, inclusive=decoded is None)
        if decoded is None:
            before, before_params = _seek(terms, back=True)
            if q:
                has_prior = (
                    project.db.execute(
                        prefix
                        + f"SELECT 1 FROM {ordered_source} WHERE {where} AND {before} LIMIT 1",
                        [
                            *cte_params,
                            *ordered_params,
                            *where_params,
                            *before_params,
                        ],
                    ).fetchone()
                    is not None
                )
            else:
                ordinal = (
                    1
                    + project.db.execute(
                        prefix
                        + f"SELECT COUNT(*) FROM {ordered_source} WHERE {where} AND {before}",
                        [
                            *cte_params,
                            *ordered_params,
                            *where_params,
                            *before_params,
                        ],
                    ).fetchone()[0]
                )
                has_prior = ordinal > 1
    order = ",".join(term.order_sql(reverse=back) for term in terms)
    rows = project.db.execute(
        prefix
        + f"SELECT r.id{',z.ordinal' if q else ''} FROM {ordered_source} WHERE {where} AND {seek} ORDER BY {order} LIMIT ?",
        [
            *cte_params,
            *ordered_params,
            *where_params,
            *seek_params,
            *order_params,
            limit + 1,
        ],
    ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    start = max(1, ordinal - len(rows)) if back else ordinal + (1 if decoded else 0)
    if back:
        rows.reverse()
    items = []
    for index, row in enumerate(rows):
        detail = project.db.execute(
            f"SELECT substr(display_title,1,256) AS title,length(display_title)>256 AS title_truncated,substr(label,1,256) AS label,lower(substr(label,-10)) AS suffix,length(label)>256 AS label_truncated,substr(mime,1,128) AS mime,CASE WHEN sk='text' THEN length(sv) END AS character_count,CASE WHEN sk IN ('text','json') THEN sv IS NOT NULL AND sv<>'' ELSE 0 END AS source_present FROM ({descriptor})",
            [*descriptor_params, int(row["id"])],
        ).fetchone()
        mime = (detail["mime"] or "").lower()
        label = detail["label"]
        suffix = detail["suffix"] or ""
        kind = "annotated_text"
        if source["type"] in {"file", "image", "audio", "video"}:
            kind = "other"
            if "pdf" in mime or suffix.endswith(".pdf"):
                kind = "pdf"
            else:
                for candidate in ("image", "audio", "video"):
                    if source["type"] == candidate or mime.startswith(candidate + "/"):
                        kind = candidate
                        break
                if kind == "other" and (
                    mime.startswith("text/")
                    or not mime
                    and suffix.endswith(
                        (".txt", ".md", ".markdown", ".log", ".csv", ".tsv", ".json")
                    )
                ):
                    kind = "text"
        rank = int(row["ordinal"]) if q else start + index
        items.append(
            dict(
                row_id=int(row["id"]),
                ordinal=rank,
                title=detail["title"] or f"Row {rank}",
                title_truncated=bool(detail["title_truncated"]),
                source_kind=kind,
                source_present=bool(detail["source_present"]),
                source_label=label,
                source_label_truncated=bool(detail["label_truncated"]),
                character_count=detail["character_count"]
                if kind == "annotated_text"
                else None,
            )
        )

    def token(item, backwards):
        return _encode(
            dict(
                scope=fingerprint,
                row=item["row_id"],
                ordinal=item["ordinal"],
                back=backwards,
                date=today,
            )
        )

    return dict(
        title_column_id=title_column_id,
        items=items,
        next_cursor=token(items[-1], False)
        if items and (more if not back else True)
        else None,
        previous_cursor=token(items[0], True)
        if items and (more if back else has_prior)
        else None,
    )
