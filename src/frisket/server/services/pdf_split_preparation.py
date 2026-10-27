"""Original-page OCR admission over the ordinary accounted scratch executor.

Only immutable page references live in the import preparation inventory.  Text
bodies remain in prepared_content; publishing a finished page into a child is
owned by the split writer, never by a second map operation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock

from frisket.redaction import safe_error

from frisket.actions.media import OcrParams
from frisket.engine.store.import_inventory import ImportInventory
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class PdfSplitPreparationInProgress(ValueError):
    """The same original page already has an admitted live OCR execution."""


@dataclass(frozen=True, slots=True)
class PdfSplitPageTarget:
    occurrence_key: str
    source_artifact_id: int | None
    blob_hash: str
    source_column_id: int | None
    page_number: int
    ocr_action_params: Mapping[str, Any]

    def __post_init__(self) -> None:
        digest = self.blob_hash.removeprefix("sha256:")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("source page requires an admitted SHA-256")
        if (
            not self.occurrence_key
            or (self.source_artifact_id is not None and self.source_artifact_id < 1)
            or self.page_number < 1
        ):
            raise ValueError("source page identity is incomplete")
        if self.source_column_id is not None and self.source_column_id < 1:
            raise ValueError("source column identity is invalid")
        params = OcrParams.model_validate(
            {"source": "scratch", **dict(self.ocr_action_params)}
        )
        normalized = params.model_dump(mode="json")
        normalized.pop("source")
        object.__setattr__(self, "ocr_action_params", normalized)
        object.__setattr__(self, "blob_hash", "sha256:" + digest)

    @property
    def approval_identity(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("source_artifact_id")
        return value

    @property
    def work_key(self) -> str:
        return hashlib.sha256(
            _json(
                {
                    "blob_hash": self.blob_hash,
                    "page": self.page_number,
                    "params": dict(self.ocr_action_params),
                }
            ).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class PdfSplitChildBinding:
    operation_id: int
    generation: str
    session_id: str
    occurrence_key: str
    work_key: str
    row_id: int
    artifact_id: int
    source_column_id: int
    text_column_id: int
    page_number: int
    expected_source_value: Any
    expected_text_value: Any


@dataclass(frozen=True, slots=True)
class PdfSplitPageWork:
    work_key: str
    operation_id: int
    generation: str
    target: PdfSplitPageTarget
    prepared_ref_id: int | None


class PdfSplitPreparationStore:
    """Add bounded source-page work to the existing import inventory sidecar."""

    def __init__(self, inventory_path: Path) -> None:
        self.inventory = ImportInventory(inventory_path)
        self.db = self.inventory._db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS pdf_split_page_work (
                work_key TEXT NOT NULL, generation TEXT NOT NULL,
                operation_id INTEGER NOT NULL, target_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', prepared_ref_id INTEGER,
                job_id TEXT, receipt_id TEXT, error TEXT,
                PRIMARY KEY(work_key,generation));
            CREATE TABLE IF NOT EXISTS pdf_split_page_occurrences (
                work_key TEXT NOT NULL, generation TEXT NOT NULL,
                occurrence_key TEXT NOT NULL, target_json TEXT NOT NULL,
                PRIMARY KEY(work_key,generation,occurrence_key));
            CREATE TABLE IF NOT EXISTS pdf_split_page_bindings (
                work_key TEXT NOT NULL, generation TEXT NOT NULL,
                row_id INTEGER NOT NULL, page_number INTEGER NOT NULL,
                binding_json TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(work_key,generation,row_id,page_number));
            CREATE TABLE IF NOT EXISTS pdf_split_page_grants (
                plan_token TEXT PRIMARY KEY, grant_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pdf_split_page_quotes (
                plan_token TEXT PRIMARY KEY, grant_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pdf_split_original_samples (
                generation TEXT PRIMARY KEY, capture_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pdf_split_preparation_generations (
                generation TEXT PRIMARY KEY, plan_token TEXT NOT NULL,
                session_id TEXT NOT NULL, operation_id INTEGER NOT NULL);
        """)

    def close(self) -> None:
        self.inventory.close()

    def __enter__(self) -> PdfSplitPreparationStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def admit(
        self,
        *,
        operation_id: int,
        generation: str,
        targets: Sequence[PdfSplitPageTarget],
        bindings: Sequence[PdfSplitChildBinding] = (),
        approved_plan_token: str | None = None,
        session_id: str = "",
        force_rescan: bool = False,
    ) -> None:
        if operation_id < 0 or not generation:
            raise ValueError("preparation requires its owning operation and generation")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.db.execute(
                "SELECT plan_token FROM pdf_split_preparation_generations WHERE generation=?",
                (generation,),
            ).fetchone()
            existing_generation = self.db.execute(
                "SELECT 1 FROM pdf_split_page_work WHERE generation=? LIMIT 1",
                (generation,),
            ).fetchone()
            effective_token = approved_plan_token or (
                str(previous[0]) if previous else None
            )
            grant = (
                self._plan_grant(effective_token)
                if effective_token is not None
                else None
            )
            for target in targets:
                active = self.db.execute(
                    "SELECT generation FROM pdf_split_page_work WHERE work_key=? AND generation<>? AND status IN ('pending','running','stopping') LIMIT 1",
                    (target.work_key, generation),
                ).fetchone()
                if active is not None:
                    raise PdfSplitPreparationInProgress(
                        "This original PDF page is already being prepared; wait for it to finish."
                    )
                own = self.db.execute(
                    "SELECT 1 FROM pdf_split_page_work WHERE work_key=? AND generation=?",
                    (target.work_key, generation),
                ).fetchone()
                if own is None and existing_generation is not None and grant is None:
                    raise ValueError(
                        "Additional source pages require reviewed OCR approval"
                    )
                if (
                    grant is not None
                    and (
                        force_rescan
                        or (
                            self.cached_ref(target) is None
                            and self.cached_preview(target) is None
                        )
                    )
                    and {"work_key": target.work_key, **target.approval_identity}
                    not in grant.scope["targets"]
                ):
                    raise ValueError("source page is outside the approved plan")
            if approved_plan_token is not None:
                grant = self._plan_grant(approved_plan_token)
                if grant is None:
                    raise ValueError("source-page OCR requires an approved plan")
                for target in targets:
                    if (
                        self.cached_ref(target) is None
                        and self.cached_preview(target) is None
                        and {"work_key": target.work_key, **target.approval_identity}
                        not in grant.scope["targets"]
                    ):
                        raise ValueError("source page is outside the approved plan")
                self.db.execute(
                    "INSERT OR IGNORE INTO pdf_split_preparation_generations VALUES(?,?,?,?)",
                    (generation, approved_plan_token, session_id, operation_id),
                )
                recorded = self.db.execute(
                    "SELECT plan_token,session_id,operation_id FROM pdf_split_preparation_generations WHERE generation=?",
                    (generation,),
                ).fetchone()
                if tuple(recorded) != (approved_plan_token, session_id, operation_id):
                    raise ValueError("source-page generation authority changed")
            for target in targets:
                encoded = _json(asdict(target))
                cached = None if force_rescan else self.cached_ref(target)
                available = not force_rescan and (
                    cached is not None or self.cached_preview(target) is not None
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO pdf_split_page_work "
                    "(work_key,generation,operation_id,target_json,status,prepared_ref_id) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        target.work_key,
                        generation,
                        operation_id,
                        encoded,
                        "completed" if available else "pending",
                        cached,
                    ),
                )
                existing = self.db.execute(
                    "SELECT operation_id FROM pdf_split_page_work WHERE work_key=? AND generation=?",
                    (target.work_key, generation),
                ).fetchone()
                if int(existing[0]) != operation_id:
                    raise ValueError(
                        "preparation generation belongs to another operation"
                    )
                self.db.execute(
                    "INSERT OR IGNORE INTO pdf_split_page_occurrences VALUES(?,?,?,?)",
                    (target.work_key, generation, target.occurrence_key, encoded),
                )
            for binding in bindings:
                if (
                    binding.operation_id != operation_id
                    or binding.generation != generation
                ):
                    raise ValueError(
                        "child binding belongs to another preparation generation"
                    )
                member = self.db.execute(
                    "SELECT 1 FROM pdf_split_page_occurrences WHERE work_key=? AND generation=? AND occurrence_key=?",
                    (binding.work_key, generation, binding.occurrence_key),
                ).fetchone()
                if member is None:
                    raise ValueError("child binding has no admitted original page")
                encoded = _json(asdict(binding))
                previous = self.db.execute(
                    "SELECT binding_json FROM pdf_split_page_bindings WHERE work_key=? AND generation=? AND row_id=? AND page_number=?",
                    (binding.work_key, generation, binding.row_id, binding.page_number),
                ).fetchone()
                if previous is not None and previous[0] != encoded:
                    raise ValueError("child preparation identity changed")
                self.db.execute(
                    "INSERT OR IGNORE INTO pdf_split_page_bindings "
                    "(work_key,generation,row_id,page_number,binding_json) VALUES(?,?,?,?,?)",
                    (
                        binding.work_key,
                        generation,
                        binding.row_id,
                        binding.page_number,
                        encoded,
                    ),
                )

    def pending_count(self) -> int:
        return int(
            self.db.execute(
                "SELECT COUNT(*) FROM pdf_split_page_work WHERE status='pending'"
            ).fetchone()[0]
        )

    def pending(self, *, limit: int = 64) -> tuple[PdfSplitPageWork, ...]:
        if not 1 <= limit <= 256:
            raise ValueError("invalid source-page preparation page size")
        return tuple(
            self._work(row)
            for row in self.db.execute(
                "SELECT * FROM pdf_split_page_work WHERE status='pending' ORDER BY rowid LIMIT ?",
                (limit,),
            )
        )

    @staticmethod
    def _work(row: Any) -> PdfSplitPageWork:
        return PdfSplitPageWork(
            str(row["work_key"]),
            int(row["operation_id"]),
            str(row["generation"]),
            PdfSplitPageTarget(**json.loads(row["target_json"])),
            row["prepared_ref_id"],
        )

    def occurrences(self, work_key: str) -> tuple[str, ...]:
        return tuple(
            str(row[0])
            for row in self.db.execute(
                "SELECT DISTINCT occurrence_key FROM pdf_split_page_occurrences WHERE work_key=? ORDER BY occurrence_key",
                (work_key,),
            )
        )

    def cached_ref(self, target: PdfSplitPageTarget) -> int | None:
        row = self.db.execute(
            "SELECT prepared_ref_id FROM pdf_split_page_work WHERE work_key=? AND status='completed' "
            "AND prepared_ref_id IS NOT NULL ORDER BY rowid DESC LIMIT 1",
            (target.work_key,),
        ).fetchone()
        return int(row[0]) if row else None

    def cached_preview(self, target: PdfSplitPageTarget) -> dict[str, Any] | None:
        from frisket.server.services.pdf_imports.previews import preview_settings

        exists = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pdf_ocr_cache'"
        ).fetchone()
        if exists is None:
            return None
        _settings, settings_hash = preview_settings(target.ocr_action_params)
        row = self.db.execute(
            "SELECT payload FROM pdf_ocr_cache WHERE sha256=? AND page=? AND engine=? AND settings_hash=?",
            (
                target.blob_hash.removeprefix("sha256:"),
                target.page_number,
                target.ocr_action_params["engine"],
                settings_hash,
            ),
        ).fetchone()
        if row:
            return json.loads(row[0])
        # Older import previews omitted default false options from their hash.
        # Reuse only when their recorded effective OCR parameters still match.
        for candidate in self.db.execute(
            "SELECT payload FROM pdf_ocr_cache WHERE sha256=? AND page=? AND engine=? ORDER BY rowid DESC",
            (
                target.blob_hash.removeprefix("sha256:"),
                target.page_number,
                target.ocr_action_params["engine"],
            ),
        ):
            payload = json.loads(candidate[0])
            recorded = payload.get("ocr_action_params")
            if not isinstance(recorded, dict):
                continue
            params = OcrParams.model_validate(
                {"source": "scratch", **recorded}
            ).model_dump(mode="json")
            params.pop("source")
            if params == dict(target.ocr_action_params):
                return payload
        return None

    def claim(self, work_key: str, *, generation: str) -> bool:
        with self.db:
            return (
                self.db.execute(
                    "UPDATE pdf_split_page_work SET status='running' WHERE work_key=? AND generation=? AND status='pending'",
                    (work_key, generation),
                ).rowcount
                == 1
            )

    def finish(self, work_key: str, *, generation: str, prepared_ref_id: int) -> bool:
        if prepared_ref_id is not None and prepared_ref_id < 1:
            raise ValueError("source page preparation requires a prepared reference")
        with self.db:
            return (
                self.db.execute(
                    "UPDATE pdf_split_page_work SET status='completed',prepared_ref_id=? "
                    "WHERE work_key=? AND generation=? AND status='running'",
                    (prepared_ref_id, work_key, generation),
                ).rowcount
                == 1
            )

    def cancel_generation(self, generation: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE pdf_split_page_work SET status=CASE WHEN status IN ('running','stopping') AND job_id IS NOT NULL THEN 'stopping' ELSE 'cancelled' END WHERE generation=? AND status IN ('pending','running','stopping')",
                (generation,),
            )
            self.db.execute(
                "DELETE FROM pdf_split_preparation_generations WHERE generation=?",
                (generation,),
            )
            self.db.execute(
                "UPDATE pdf_split_page_bindings SET published=-1 WHERE generation=? AND published=0",
                (generation,),
            )

    def bindings(
        self, work_key: str, *, generation: str, after_row_id: int = 0, limit: int = 256
    ) -> tuple[PdfSplitChildBinding, ...]:
        return tuple(
            PdfSplitChildBinding(**json.loads(row[0]))
            for row in self.db.execute(
                "SELECT binding_json FROM pdf_split_page_bindings WHERE work_key=? AND generation=? AND published=0 AND row_id>? ORDER BY row_id,page_number LIMIT ?",
                (work_key, generation, after_row_id, limit),
            )
        )

    def mark_published(
        self, binding: PdfSplitChildBinding, *, applied: bool = True
    ) -> None:
        with self.db:
            self.db.execute(
                "UPDATE pdf_split_page_bindings SET published=? WHERE work_key=? AND generation=? AND row_id=? AND page_number=?",
                (
                    1 if applied else -1,
                    binding.work_key,
                    binding.generation,
                    binding.row_id,
                    binding.page_number,
                ),
            )

    def record_started(
        self, work: PdfSplitPageWork, *, job_id: str, receipt_id: str | None
    ) -> None:
        with self.db:
            self.db.execute(
                "UPDATE pdf_split_page_work SET job_id=?,receipt_id=? WHERE work_key=? AND generation=? AND status IN ('running','completed')",
                (job_id, receipt_id, work.work_key, work.generation),
            )

    def release(self, work: PdfSplitPageWork, *, error: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE pdf_split_page_work SET status=?,error=? WHERE work_key=? AND generation=? AND status='running'",
                (
                    "failed" if error else "pending",
                    error,
                    work.work_key,
                    work.generation,
                ),
            )

    def approve(self, *, grant: Any, confirmation: str | None) -> None:
        grant._validate()
        expected = self.db.execute(
            "SELECT grant_json FROM pdf_split_page_quotes WHERE plan_token=?",
            (grant.scope["plan_token"],),
        ).fetchone()
        if expected is None or str(expected[0]) != _json(grant.as_mapping()):
            raise ValueError("source-page OCR quote changed; review its exact claims")
        if (
            grant.allocation["requires_confirmation"]
            and confirmation != grant.promise_set_hash
        ):
            raise ValueError(
                "source-page OCR requires confirmation of its exact claims"
            )
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO pdf_split_page_grants VALUES(?,?)",
                (grant.scope["plan_token"], _json(grant.as_mapping())),
            )

    def _plan_grant(self, plan_token: str):
        from frisket.server.services.pdf_import_ocr_approval import PdfImportOcrGrant

        row = self.db.execute(
            "SELECT grant_json FROM pdf_split_page_grants WHERE plan_token=?",
            (plan_token,),
        ).fetchone()
        return PdfImportOcrGrant.from_mapping(json.loads(row[0])) if row else None

    def grant(self, generation: str):
        row = self.db.execute(
            "SELECT plan_token FROM pdf_split_preparation_generations WHERE generation=?",
            (generation,),
        ).fetchone()
        return self._plan_grant(str(row[0])) if row else None


class PdfSplitPreparationAdapter:
    """Execute one approved original page and fan out its stored page reference.

    The only injected seam is the owning split's transactional child publisher.
    Source paths, artifact validation, caching, estimates and receipts use their
    existing concrete authorities.
    """

    def __init__(
        self,
        workspace: Any,
        *,
        preview_runs: Any,
        inventory_path: Path,
        project_id: str,
        publish_child_page: Callable[[PdfSplitChildBinding, int], bool] | None = None,
        admitted_sources: Mapping[str, Any] | None = None,
    ) -> None:
        self.workspace = workspace
        self.preview_runs = preview_runs
        self.inventory_path = inventory_path
        self.project_id = project_id
        self.publish_child_page = publish_child_page
        self.admitted_sources = dict(admitted_sources or {})
        self._lock = FileLock(
            str(inventory_path.with_name(".pdf-source-preparation.lock"))
        )

    def _source(self, target: PdfSplitPageTarget) -> Any:
        project = self.workspace.get(self.project_id)
        source = self.admitted_sources.get(target.occurrence_key)
        if source is not None and any(
            (
                source.sha256.removeprefix("sha256:")
                != target.blob_hash.removeprefix("sha256:"),
                not source.page_start <= target.page_number <= source.page_end,
                source.source_column_id != target.source_column_id,
            )
        ):
            raise ValueError("admitted original page identity changed")
        if target.source_artifact_id is None:
            if source is None:
                raise ValueError("admitted original page identity changed")
            return {
                "filename": source.filename,
                "page_count": source.page_count,
                "mime": getattr(source, "mime", "application/pdf"),
            }
        artifact = project.db.execute(
            "SELECT blob_hash,page_count,source_column_id,filename,media_type AS mime FROM source_artifacts WHERE id=?",
            (target.source_artifact_id,),
        ).fetchone()
        if (
            artifact is None
            or str(artifact["blob_hash"]).removeprefix("sha256:")
            != target.blob_hash.removeprefix("sha256:")
            or not isinstance(artifact["page_count"], int)
            or not 1 <= target.page_number <= artifact["page_count"]
            or artifact["source_column_id"] != target.source_column_id
        ):
            raise ValueError("admitted original page identity changed")
        return artifact

    def cached_reference(self, target: PdfSplitPageTarget) -> int | None:
        """Find retained original pages after their review inventory is discarded."""
        if (
            target.source_artifact_id is None
            and getattr(
                self.admitted_sources.get(target.occurrence_key), "source_row_id", None
            )
            is None
        ):
            return None
        project = self.workspace.get(self.project_id)
        for row in project.db.execute(
            "SELECT ref.id,version.positions_json FROM prepared_content_refs ref "
            "JOIN prepared_content_sets content ON content.id=ref.set_id "
            "JOIN source_artifacts source ON source.id=content.source_artifact_id "
            "JOIN prepared_content_set_pages member ON member.set_id=content.id "
            "JOIN prepared_page_versions version ON version.id=member.version_id "
            "WHERE source.blob_hash IN (?,?) AND member.page_number=? "
            "AND (ref.page_number IS NULL OR ref.page_number=?) ORDER BY ref.id DESC",
            (
                target.blob_hash,
                target.blob_hash.removeprefix("sha256:"),
                target.page_number,
                target.page_number,
            ),
        ):
            positions = json.loads(row[1]) if row[1] else {}
            if positions.get("ocr_action_params") == dict(target.ocr_action_params):
                return int(row[0])
        return None

    def _prepare(
        self,
        work: PdfSplitPageWork,
        *,
        on_page: Callable | None = None,
        request_context: Any = None,
    ):
        artifact = self._source(work.target)
        project = self.workspace.get(self.project_id)
        lease = ExitStack()
        try:
            path = lease.enter_context(
                self.admitted_sources[work.target.occurrence_key].open_source()
                if work.target.source_artifact_id is None
                else project.blob_store.materialize(
                    work.target.blob_hash.removeprefix("sha256:")
                )
            )
            size = path.stat().st_size
            prepared = self.preview_runs.prepare_ocr_scratch_path(
                self.project_id,
                path,
                media_digest=work.target.blob_hash.removeprefix("sha256:"),
                media_size=size,
                payload={
                    **dict(work.target.ocr_action_params),
                    "filename": artifact["filename"],
                    "mime": artifact["mime"],
                    "pages": [work.target.page_number],
                    "searchable_pdf": False,
                },
                on_page=on_page,
                max_pages=1,
                request_context=request_context,
            )
            return lease, prepared
        except BaseException:
            lease.close()
            raise

    def quote(
        self,
        *,
        targets: Sequence[PdfSplitPageTarget],
        plan_token: str,
        request_context: Any = None,
        force_rescan: bool = False,
    ) -> dict[str, Any]:
        from frisket.engine.runner.confirmation_context import ConsentQuote
        from frisket.execution.resolve_for_action import promise_rows, target_snapshot
        from frisket.server.services.action_preview_runs import _scratch_claims
        from frisket.server.services.pdf_import_ocr_approval import (
            _route_snapshot,
            source_page_ocr_grant,
        )

        if not plan_token or not targets:
            raise ValueError("OCR quote requires its admitted source plan")
        project = self.workspace.get(self.project_id)
        children: list[dict[str, Any]] = []
        roster: list[dict[str, Any]] = []
        params = None
        seen = set()
        with PdfSplitPreparationStore(self.inventory_path) as store:
            for target in targets:
                self._source(target)
                active = store.db.execute(
                    "SELECT 1 FROM pdf_split_page_work WHERE work_key=? AND status IN ('pending','running','stopping') LIMIT 1",
                    (target.work_key,),
                ).fetchone()
                if active is not None:
                    raise PdfSplitPreparationInProgress(
                        "This original PDF page is already being prepared; wait for it to finish."
                    )
                if not force_rescan and (
                    store.cached_ref(target) is not None
                    or self.cached_reference(target) is not None
                    or store.cached_preview(target) is not None
                ):
                    continue
                roster.append({"work_key": target.work_key, **target.approval_identity})
                if target.work_key in seen:
                    continue
                seen.add(target.work_key)
                if params is not None and params != dict(target.ocr_action_params):
                    raise ValueError(
                        "source-page OCR engine/settings must be reviewed together"
                    )
                params = dict(target.ocr_action_params)
                work = PdfSplitPageWork(target.work_key, 0, "", target, None)
                lease, prepared = self._prepare(work, request_context=request_context)
                try:
                    plan = prepared[0]
                    resolved, estimate, _uncovered = _scratch_claims(project, plan)
                    children.append(
                        {
                            "child_key": work.work_key,
                            "spec": plan.spec,
                            "target": target_snapshot(resolved.resolution),
                            "route": _route_snapshot(resolved),
                            "promises": promise_rows(resolved.promise_set),
                            "quote": ConsentQuote.from_estimate(estimate).model_dump(
                                mode="json"
                            ),
                        }
                    )
                finally:
                    lease.close()
        if not children:
            return {
                "pages": 0,
                "estimate": {
                    "cost": 0,
                    "billed_cost": 0,
                    "requires_confirmation": False,
                },
                "authorization": None,
            }
        quoted = source_page_ocr_grant(
            project_id=self.project_id,
            plan_token=plan_token,
            project=project,
            composition=prepared[3],
            consent_coverage=plan.consent_coverage,
            targets=roster,
            children=children,
            ocr_action_params=params or {},
        )
        with PdfSplitPreparationStore(self.inventory_path) as store, store.db:
            store.db.execute(
                "INSERT OR REPLACE INTO pdf_split_page_quotes VALUES(?,?)",
                (plan_token, _json(quoted.authorization)),
            )
        return {"pages": len(children), **quoted.as_mapping()}

    def approve(self, quoted: Mapping[str, Any], *, confirmation: str | None) -> None:
        from frisket.server.services.pdf_import_ocr_approval import PdfImportOcrGrant

        if quoted["authorization"] is None:
            return
        grant = PdfImportOcrGrant.from_mapping(quoted["authorization"])
        if grant.scope.get("project_id") != self.project_id:
            raise ValueError("source-page approval belongs to another project")
        with PdfSplitPreparationStore(self.inventory_path) as store:
            store.approve(grant=grant, confirmation=confirmation)

    def _generation_current(
        self,
        store: PdfSplitPreparationStore,
        work: PdfSplitPageWork,
        *,
        explicit_preview: bool = False,
    ) -> bool:
        row = store.db.execute(
            "SELECT session_id,operation_id FROM pdf_split_preparation_generations WHERE generation=?",
            (work.generation,),
        ).fetchone()
        if row is None or int(row[1]) != work.operation_id:
            return False
        if explicit_preview and not row[0]:
            if work.target.source_artifact_id is None:
                return True
            op = (
                self.workspace.get(self.project_id)
                .db.execute("SELECT status FROM ops WHERE id=?", (work.operation_id,))
                .fetchone()
            )
            return op is not None and op[0] == "applied"
        from frisket.engine.store.import_sessions import (
            import_session_generation_is_applied,
        )

        return import_session_generation_is_applied(
            self.workspace.get(self.project_id).db,
            session_id=str(row[0]),
            writer_authority=work.generation,
            op_id=work.operation_id,
        )

    def _complete(
        self,
        work: PdfSplitPageWork,
        page: Mapping[str, Any],
        *,
        explicit_preview: bool = False,
    ) -> None:
        if page.get("page") != work.target.page_number:
            raise ValueError("OCR returned another original page")
        project = self.workspace.get(self.project_id)
        self._source(work.target)
        # Provider call/accounting already belongs to the ordinary scratch
        # receipt. The split operation owns only the published page reference.
        with PdfSplitPreparationStore(self.inventory_path) as store:
            current = store.db.execute(
                "SELECT status FROM pdf_split_page_work WHERE work_key=? AND generation=?",
                (work.work_key, work.generation),
            ).fetchone()
            if current is None or current[0] != "running":
                return
            sample = (
                store.db.execute(
                    "SELECT capture_json FROM pdf_split_original_samples WHERE generation=?",
                    (work.generation,),
                ).fetchone()
                if explicit_preview
                else None
            )
            if sample is not None:
                from frisket.engine.store.pdf_ocr_sample import publish_pdf_ocr_sample

                receipt_id = page.get("receipt_id")
                if not isinstance(receipt_id, str) or not receipt_id:
                    raise ValueError(
                        "original sample publication requires its running OCR receipt"
                    )
                published = publish_pdf_ocr_sample(
                    project, json.loads(sample[0]), page=page, receipt_id=receipt_id
                )
                if published is not None:
                    store.finish(
                        work.work_key,
                        generation=work.generation,
                        prepared_ref_id=int(published["prepared_ref_id"]),
                    )
                else:
                    store.release(
                        work, error="The original source or Text changed during OCR."
                    )
                return
            if work.target.source_artifact_id is None:
                if not explicit_preview:
                    raise ValueError(
                        "automatic OCR requires its retained source artifact"
                    )
                from frisket.server.services.pdf_imports.inventory import (
                    PdfDraftInventory,
                )
                from frisket.server.services.pdf_imports.previews import (
                    preview_settings,
                )

                _settings, settings_hash = preview_settings(
                    work.target.ocr_action_params
                )
                with PdfDraftInventory(self.inventory_path) as draft:
                    draft.cache_ocr(
                        work.target.blob_hash.removeprefix("sha256:"),
                        work.target.page_number,
                        work.target.ocr_action_params["engine"],
                        settings_hash,
                        {
                            **dict(page),
                            "engine": work.target.ocr_action_params["engine"],
                            "ocr_action_params": dict(work.target.ocr_action_params),
                        },
                    )
                store.finish(
                    work.work_key, generation=work.generation, prepared_ref_id=None
                )
                return
            with project.db:
                project.db.execute("BEGIN IMMEDIATE")
                if not self._generation_current(
                    store, work, explicit_preview=explicit_preview
                ):
                    store.cancel_generation(work.generation)
                    return
                ref = PreparedContentStore(project).stage_reference(
                    source_artifact_id=work.target.source_artifact_id,
                    producing_op_id=work.operation_id,
                    pages=[
                        PreparedPageDraft(
                            work.target.page_number,
                            str(page.get("text") or ""),
                            positions={
                                "engine": work.target.ocr_action_params["engine"],
                                "ocr_action_params": dict(
                                    work.target.ocr_action_params
                                ),
                                "width": page.get("width"),
                                "height": page.get("height"),
                                "blocks": list(page.get("blocks") or []),
                                "source_artifact_id": work.target.source_artifact_id,
                                "source_page_number": work.target.page_number,
                            },
                        )
                    ],
                    page_number=work.target.page_number,
                    allow_partial=True,
                )
            if not store.finish(
                work.work_key, generation=work.generation, prepared_ref_id=ref.ref_id
            ):
                return
        self.fanout(work)

    def fanout(self, work: PdfSplitPageWork) -> int:
        if self.publish_child_page is None:
            return 0
        published = 0
        with PdfSplitPreparationStore(self.inventory_path) as store:
            if not self._generation_current(store, work):
                with store.db:
                    store.db.execute(
                        "UPDATE pdf_split_page_bindings SET published=-1 WHERE work_key=? AND generation=? AND published=0",
                        (work.work_key, work.generation),
                    )
                return 0
            row = store.db.execute(
                "SELECT status,prepared_ref_id FROM pdf_split_page_work WHERE work_key=? AND generation=?",
                (work.work_key, work.generation),
            ).fetchone()
            if row is None or row[0] != "completed" or row[1] is None:
                return 0
            after_row_id = 0
            while bindings := store.bindings(
                work.work_key, generation=work.generation, after_row_id=after_row_id
            ):
                for binding in bindings:
                    applied = self.publish_child_page(binding, int(row[1]))
                    store.mark_published(binding, applied=applied)
                    if applied:
                        published += 1
                after_row_id = bindings[-1].row_id
        return published

    def resume_fanout(self, *, generation: str, limit: int = 64) -> int:
        with PdfSplitPreparationStore(self.inventory_path) as store:
            works = tuple(
                store._work(row)
                for row in store.db.execute(
                    "SELECT work.* FROM pdf_split_page_work work WHERE work.generation=? AND work.status='completed' "
                    "AND EXISTS(SELECT 1 FROM pdf_split_page_bindings binding WHERE binding.work_key=work.work_key "
                    "AND binding.generation=work.generation AND binding.published=0) ORDER BY work.rowid LIMIT ?",
                    (generation, limit),
                )
            )
        total = 0
        for work in works:
            with PdfSplitPreparationStore(self.inventory_path) as store:
                row = store.db.execute(
                    "SELECT job_id FROM pdf_split_page_work WHERE work_key=? AND generation=?",
                    (work.work_key, generation),
                ).fetchone()
            job = (
                self.preview_runs._registry.get(self.project_id, str(row[0]))
                if row and row[0]
                else None
            )
            if job is not None and job.finished_at is None:
                continue
            total += self.fanout(work)
        return total

    def start_pending(
        self, *, generation: str, limit: int = 4, request_context: Any = None
    ) -> tuple[dict[str, Any], ...]:
        with self._lock:
            self.refresh(generation=generation)
            return self._start_pending(
                generation=generation, limit=limit, request_context=request_context
            )

    def _start_pending(
        self,
        *,
        generation: str,
        limit: int,
        request_context: Any = None,
        explicit_preview: bool = False,
        force_rescan: bool = False,
    ) -> tuple[dict[str, Any], ...]:
        from dataclasses import replace
        from frisket.server.services.pdf_import_ocr_approval import (
            source_page_ocr_authorizer,
        )

        responses = []
        with PdfSplitPreparationStore(self.inventory_path) as store:
            grant = store.grant(generation)
            original_sample = (
                store.db.execute(
                    "SELECT 1 FROM pdf_split_original_samples WHERE generation=?",
                    (generation,),
                ).fetchone()
                is not None
            )
            if not 1 <= limit <= 256:
                raise ValueError("invalid source-page worker limit")
            active = int(
                store.db.execute(
                    "SELECT COUNT(*) FROM pdf_split_page_work WHERE generation=? AND status='running'",
                    (generation,),
                ).fetchone()[0]
            )
            limit = max(0, limit - active)
            works = tuple(
                store._work(row)
                for row in store.db.execute(
                    "SELECT * FROM pdf_split_page_work WHERE generation=? AND status='pending' ORDER BY rowid LIMIT ?",
                    (generation, limit),
                )
            )
        for work in works:
            if grant is None:
                raise ValueError("source-page OCR requires its reviewed approval")
            with PdfSplitPreparationStore(self.inventory_path) as store:
                if not self._generation_current(
                    store, work, explicit_preview=explicit_preview
                ):
                    store.cancel_generation(generation)
                    break
            authorizer = source_page_ocr_authorizer(
                grant,
                project_id=self.project_id,
                plan_token=str(grant.scope["plan_token"]),
                target=work.target.approval_identity,
                work_key=work.work_key,
            )
            with PdfSplitPreparationStore(self.inventory_path) as store:
                if not store.claim(work.work_key, generation=generation):
                    continue
            lease = None
            flight = None
            accepted = False
            try:
                flight_directory = (
                    self.workspace.get(self.project_id).path
                    / ".imports"
                    / ".pdf-page-ocr-locks"
                )
                flight_directory.mkdir(parents=True, exist_ok=True)
                flight = FileLock(
                    str(flight_directory / f"{work.work_key}.lock"), thread_local=False
                )
                from filelock import Timeout

                try:
                    flight.acquire(timeout=0)
                except Timeout as exc:
                    raise PdfSplitPreparationInProgress(
                        "This original PDF page is already being prepared; wait for it to finish."
                    ) from exc
                cached_ref = (
                    None if force_rescan else self.cached_reference(work.target)
                )
                if cached_ref is not None:
                    with PdfSplitPreparationStore(self.inventory_path) as store:
                        capture = store.db.execute(
                            "SELECT capture_json FROM pdf_split_original_samples WHERE generation=?",
                            (generation,),
                        ).fetchone()
                    if capture is not None:
                        from frisket.engine.store.pdf_ocr_sample import (
                            publish_cached_pdf_ocr_sample,
                        )

                        copied = publish_cached_pdf_ocr_sample(
                            self.workspace.get(self.project_id),
                            json.loads(capture[0]),
                            prepared_ref_id=cached_ref,
                            project_id=self.project_id,
                        )
                        cached_ref = int(copied["prepared_ref_id"])
                    with PdfSplitPreparationStore(self.inventory_path) as store:
                        store.finish(
                            work.work_key,
                            generation=generation,
                            prepared_ref_id=cached_ref,
                        )
                    if capture is None:
                        self.fanout(work)
                    flight.release()
                    continue
                lease, prepared = self._prepare(
                    work,
                    on_page=lambda page, work=work: self._complete(
                        work, page, explicit_preview=explicit_preview
                    ),
                    request_context=request_context,
                )
                plan, _source, router, composition, execution_context = prepared
                run = plan.run

                async def run_leased(
                    context, run=run, lease=lease, flight=flight, work=work
                ):
                    failure = None
                    try:
                        return await run(context)
                    except BaseException as exc:
                        failure = exc
                        raise
                    finally:
                        try:
                            with (
                                PdfSplitPreparationStore(self.inventory_path) as store,
                                store.db,
                            ):
                                state = store.db.execute(
                                    "SELECT status FROM pdf_split_page_work WHERE work_key=? AND generation=?",
                                    (work.work_key, work.generation),
                                ).fetchone()
                                if state is not None and state[0] in {
                                    "running",
                                    "stopping",
                                }:
                                    terminal = (
                                        "cancelled"
                                        if context.cancelled or state[0] == "stopping"
                                        else "failed"
                                    )
                                    store.db.execute(
                                        "UPDATE pdf_split_page_work SET status=?,error=? WHERE work_key=? AND generation=?",
                                        (
                                            terminal,
                                            safe_error(
                                                "pdf_source_preparation_failed",
                                                failure,
                                                max_chars=500,
                                            ).text
                                            if failure
                                            else None,
                                            work.work_key,
                                            work.generation,
                                        ),
                                    )
                                    store.db.execute(
                                        "UPDATE pdf_split_page_bindings SET published=-1 WHERE work_key=? AND generation=? AND published=0",
                                        (work.work_key, work.generation),
                                    )
                        finally:
                            lease.close()
                            flight.release()

                started = self.preview_runs.start_scratch_preview(
                    self.project_id,
                    replace(plan, run=run_leased),
                    confirmation=None,
                    router=router,
                    composition=composition,
                    execution_context=execution_context,
                    total=1,
                    replacement_key=f"pdf-split-page:{generation}:{work.work_key}",
                    trusted_consent_authorizer=authorizer,
                    trusted_consent_principal=grant.actor,
                    require_receipt=explicit_preview and original_sample,
                )
                if started.status_code != 202:
                    lease.close()
                    flight.release()
                    with PdfSplitPreparationStore(self.inventory_path) as store:
                        store.release(work)
                    responses.append(dict(started.payload))
                    continue
                accepted = True
                job_id = str(started.payload["preview_id"])
                with PdfSplitPreparationStore(self.inventory_path) as store:
                    store.record_started(work, job_id=job_id, receipt_id=None)
                job = self.preview_runs._registry.get(self.project_id, job_id)
                with PdfSplitPreparationStore(self.inventory_path) as store:
                    store.record_started(
                        work,
                        job_id=job_id,
                        receipt_id=job.receipt.receipt_id
                        if job and job.receipt
                        else None,
                    )
                responses.append(dict(started.payload))
            except BaseException as exc:
                if accepted:
                    # The ordinary executor owns its accepted job and lease.
                    # Recovery may cancel uncertain tracking, never re-bill it.
                    responses.append(dict(started.payload))
                    continue
                if lease is not None:
                    lease.close()
                if flight is not None:
                    flight.release()
                with PdfSplitPreparationStore(self.inventory_path) as store:
                    store.release(
                        work,
                        error=None
                        if isinstance(exc, PdfSplitPreparationInProgress)
                        else safe_error(
                            "pdf_source_preparation_failed", exc, max_chars=500
                        ).text,
                    )
                raise
        return tuple(responses)

    def start_preview(
        self,
        *,
        targets: Sequence[PdfSplitPageTarget],
        plan_token: str,
        operation_id: int,
        generation: str,
        confirmation: str | None,
        request_context: Any = None,
        force_rescan: bool = False,
        original_publication: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Run explicitly requested sample OCR under its earlier preparation op."""
        quoted = self.quote(
            targets=targets,
            plan_token=plan_token,
            request_context=request_context,
            force_rescan=force_rescan,
        )
        self.approve(quoted, confirmation=confirmation)
        if quoted["authorization"] is None:
            return ()
        with PdfSplitPreparationStore(self.inventory_path) as store:
            store.admit(
                operation_id=operation_id,
                generation=generation,
                targets=targets,
                approved_plan_token=plan_token,
                force_rescan=force_rescan,
            )
            if original_publication is not None:
                with store.db:
                    store.db.execute(
                        "INSERT OR REPLACE INTO pdf_split_original_samples VALUES(?,?)",
                        (generation, _json(dict(original_publication))),
                    )
        with self._lock:
            return self._start_pending(
                generation=generation,
                limit=min(256, len(targets)),
                request_context=request_context,
                explicit_preview=True,
                force_rescan=force_rescan,
            )

    def stop(self, *, generation: str) -> None:
        """Cancel unattempted work; ordinary scratch receipts settle attempted work."""
        with self._lock, PdfSplitPreparationStore(self.inventory_path) as store:
            jobs = [
                str(row[0])
                for row in store.db.execute(
                    "SELECT job_id FROM pdf_split_page_work WHERE generation=? AND status='running' AND job_id IS NOT NULL",
                    (generation,),
                )
            ]
            store.cancel_generation(generation)
        for job_id in jobs:
            self.preview_runs._registry.cancel(self.project_id, job_id)

    def refresh(self, *, generation: str) -> None:
        """Project executor termination without retrying uncertain provider work."""
        with self._lock, PdfSplitPreparationStore(self.inventory_path) as store:
            with store.db:
                store.db.execute(
                    "UPDATE pdf_split_page_bindings AS binding SET published=-1 WHERE generation=? AND published=0 "
                    "AND EXISTS (SELECT 1 FROM pdf_split_page_work work WHERE work.generation=binding.generation "
                    "AND work.work_key=binding.work_key AND work.status IN ('failed','cancelled'))",
                    (generation,),
                )
            rows = store.db.execute(
                "SELECT * FROM pdf_split_page_work WHERE generation=? AND status IN ('running','stopping') ORDER BY rowid LIMIT 256",
                (generation,),
            ).fetchall()
            for row in rows:
                job = (
                    self.preview_runs._registry.get(self.project_id, str(row["job_id"]))
                    if row["job_id"] is not None
                    else None
                )
                if job is not None and job.finished_at is None:
                    continue
                # A completed successful page was already persisted by on_page.
                # Missing jobs after restart are cancelled rather than re-billed.
                status = (
                    "cancelled"
                    if row["status"] == "stopping"
                    or job is None
                    or job.status == "cancelled"
                    else "failed"
                )
                with store.db:
                    store.db.execute(
                        "UPDATE pdf_split_page_work SET status=? WHERE work_key=? AND generation=? AND status IN ('running','stopping')",
                        (status, row["work_key"], generation),
                    )
                    store.db.execute(
                        "UPDATE pdf_split_page_bindings SET published=-1 WHERE work_key=? AND generation=? AND published=0",
                        (row["work_key"], generation),
                    )

    def progress(self, *, generation: str) -> dict[str, Any]:
        self.refresh(generation=generation)
        with PdfSplitPreparationStore(self.inventory_path) as store:
            counts = {
                str(row[0]): int(row[1])
                for row in store.db.execute(
                    "SELECT status,COUNT(*) FROM pdf_split_page_work WHERE generation=? GROUP BY status",
                    (generation,),
                )
            }
            pages = sum(counts.values())
            counts["read_pages"] = counts.get("completed", 0)
            counts["pending_bindings"] = int(
                store.db.execute(
                    "SELECT COUNT(*) FROM pdf_split_page_bindings WHERE generation=? AND published=0",
                    (generation,),
                ).fetchone()[0]
            )
            counts["completed"] = int(
                store.db.execute(
                    "SELECT COUNT(*) FROM pdf_split_page_work work WHERE generation=? AND status='completed' AND NOT EXISTS (SELECT 1 FROM pdf_split_page_bindings binding WHERE binding.generation=work.generation AND binding.work_key=work.work_key AND published=0)",
                    (generation,),
                ).fetchone()[0]
            )
            return {"generation": generation, "pages": pages, **counts}
