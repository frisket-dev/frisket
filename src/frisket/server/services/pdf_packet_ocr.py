"""Reviewed page OCR selection and consent shared by packet apply callers."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.actions.media import OcrParams
from frisket.documents.text_quality import assess_text_quality
from frisket.engine.pdf_text import extract_pdf_text_batch, positioned_tokens_text
from frisket.server.services.pdf_packet_sources import AdmittedPacketSource
from frisket.server.services.pdf_split_preparation import (
    PdfSplitPageTarget,
    PdfSplitPreparationAdapter,
)


@dataclass(frozen=True)
class PdfPacketOcrQuote:
    public: Mapping[str, Any]
    binding_hash: str
    quoted: Mapping[str, Any]
    native_files: Mapping[str, Mapping[int, Path]]

    def snapshot(self, root: Path) -> dict[str, Any]:
        return {
            "public": dict(self.public),
            "binding_hash": self.binding_hash,
            "quoted": dict(self.quoted),
            "native_files": {
                key: {
                    str(page): str(path.resolve().relative_to(root.resolve()))
                    for page, path in pages.items()
                }
                for key, pages in self.native_files.items()
            },
        }

    @classmethod
    def restore(cls, value: Mapping[str, Any], root: Path) -> PdfPacketOcrQuote:
        files = {}
        for key, pages in value.get("native_files", {}).items():
            files[key] = {}
            for page, relative in pages.items():
                path = (root / str(relative)).resolve()
                path.relative_to(root.resolve())
                if not path.is_file():
                    raise ValueError(
                        "saved native page is unavailable; reopen PDF setup"
                    )
                files[key][int(page)] = path
        return cls(
            dict(value["public"]),
            str(value["binding_hash"]),
            dict(value["quoted"]),
            files,
        )


def packet_ocr_params(selection: Mapping[str, Any]) -> dict[str, Any]:
    engine = str(selection.get("engine") or "")
    model = selection.get("model")
    if selection.get("kind") == "engine_model" and model is not None:
        if engine != "llm":
            raise ValueError("OCR model selection does not match its engine")
        engine = str(model)
    elif selection.get("kind") not in {"engine", "engine_model"}:
        raise ValueError("invalid OCR engine selection")
    value = OcrParams.model_validate(
        {"source": "scratch", "engine": engine}
    ).model_dump(mode="json")
    value.pop("source")
    return value


def _binding(
    sources: Sequence[AdmittedPacketSource],
    revision: int,
    policy: str,
    params: Mapping[str, Any],
) -> str:
    if policy not in {"none", "missing", "missing_or_suspicious", "all"}:
        raise ValueError("invalid PDF OCR policy")
    value = {
        "revision": revision,
        "policy": policy,
        "params": dict(params),
        "sources": [
            {
                "key": source.key,
                "sha256": source.sha256.removeprefix("sha256:"),
                "start": source.page_start,
                "end": source.page_end,
                "column": source.source_column_id,
                "sheet": source.source_sheet_id,
                "row": source.source_row_id,
                "value": source.frozen_value,
            }
            for source in sources
        ],
    }
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _read_native_missing(source, path, directory, page_text, accepted, should_cancel):
    """Persist complete page batches, retaining only one bounded response."""
    directory.mkdir(parents=True, exist_ok=True)
    missing = [
        page
        for page in range(source.page_start, source.page_end + 1)
        if (source.key, page) not in page_text
        and (source.key, page) not in accepted
        and not (directory / f"{page}.json").is_file()
    ]
    cursor = 0
    while cursor < len(missing):
        if should_cancel is not None and should_cancel():
            raise InterruptedError("PDF OCR quotation was cancelled")
        start = missing[cursor]
        count = 1
        while (
            cursor + count < len(missing)
            and count < 64
            and missing[cursor + count] == start + count
        ):
            count += 1
        batch = asyncio.run(
            extract_pdf_text_batch(
                path, start_page=start, max_pages=count, should_cancel=should_cancel
            )
        )
        for native in batch.pages:
            payload = {
                "text": positioned_tokens_text(native.tokens),
                "engine": "native",
                "positions": {
                    "engine": "native",
                    "width": native.width,
                    "height": native.height,
                    "blocks": [
                        {
                            "text": token.text,
                            "bbox": {
                                "space": "page_normalized",
                                **token.box.model_dump(),
                            },
                        }
                        for token in native.tokens
                    ],
                },
            }
            (directory / f"{native.page}.json").write_text(
                json.dumps(payload, separators=(",", ":"))
            )
        cursor += len(batch.pages)


def _cached_page(adapter, store, target):
    payload = store.cached_preview(target)
    if payload is not None:
        return {
            "text": str(payload.get("text") or ""),
            "engine": target.ocr_action_params["engine"],
            "positions": {
                "engine": target.ocr_action_params["engine"],
                "ocr_action_params": dict(target.ocr_action_params),
                "width": payload.get("width"),
                "height": payload.get("height"),
                "blocks": payload.get("blocks") or [],
            },
        }
    ref_id = store.cached_ref(target) or adapter.cached_reference(target)
    if ref_id is None:
        return None
    row = (
        adapter.workspace.get(adapter.project_id)
        .db.execute(
            "SELECT version.prepared_text,version.positions_json FROM prepared_content_refs ref "
            "JOIN prepared_content_set_pages member ON member.set_id=ref.set_id AND member.page_number=? "
            "JOIN prepared_page_versions version ON version.id=member.version_id WHERE ref.id=? "
            "AND (ref.page_number IS NULL OR ref.page_number=member.page_number)",
            (target.page_number, ref_id),
        )
        .fetchone()
    )
    if row is None:
        return None
    return {
        "text": str(row[0]),
        "engine": target.ocr_action_params["engine"],
        "positions": json.loads(row[1]) if row[1] else None,
    }


class PdfPacketOcrCoordinator:
    def __init__(
        self, workspace: Any, *, preview_runs: Any, inventory_path: Path
    ) -> None:
        self.workspace = workspace
        self.preview_runs = preview_runs
        self.inventory_path = inventory_path

    def quote(
        self,
        *,
        project_id: str,
        sources: Sequence[AdmittedPacketSource],
        revision: int,
        ocr_policy: str,
        ocr_selection: Mapping[str, Any],
        page_text: Mapping[tuple[str, int], str],
        accepted_ocr_pages: Collection[tuple[str, int]] = (),
        manual_text_sources: Collection[str] = (),
        request_context: Any = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> PdfPacketOcrQuote:
        params = packet_ocr_params(ocr_selection)
        binding_hash = _binding(sources, revision, ocr_policy, params)
        adapter = PdfSplitPreparationAdapter(
            self.workspace,
            preview_runs=self.preview_runs,
            inventory_path=self.inventory_path,
            project_id=project_id,
            admitted_sources={source.key: source for source in sources},
        )
        targets = []
        native_files: dict[str, dict[int, Path]] = {}
        accepted = set(accepted_ocr_pages)
        from frisket.server.services.pdf_split_preparation import (
            PdfSplitPreparationStore,
        )

        with PdfSplitPreparationStore(self.inventory_path) as store:
            for source in sources:
                for page in range(source.page_start, source.page_end + 1):
                    if (source.key, page) in accepted:
                        continue
                    target = PdfSplitPageTarget(
                        source.key,
                        None,
                        "sha256:" + source.sha256.removeprefix("sha256:"),
                        source.source_column_id,
                        page,
                        params,
                    )
                    cached = _cached_page(adapter, store, target)
                    if cached is None:
                        continue
                    directory = (
                        self.inventory_path.parent
                        / "prepared"
                        / source.sha256.removeprefix("sha256:")
                    )
                    directory.mkdir(parents=True, exist_ok=True)
                    path = directory / f"{target.work_key}.json"
                    path.write_text(json.dumps(cached, separators=(",", ":")))
                    native_files.setdefault(source.key, {})[page] = path
                    accepted.add((source.key, page))
        for source in sources:
            if ocr_policy == "none":
                break
            with source.open_source() as path:
                directory = (
                    self.inventory_path.parent
                    / "native"
                    / source.sha256.removeprefix("sha256:")
                )
                if ocr_policy != "all" and source.mime == "application/pdf":
                    _read_native_missing(
                        source, path, directory, page_text, accepted, should_cancel
                    )
                for page in range(source.page_start, source.page_end + 1):
                    if should_cancel is not None and should_cancel():
                        raise InterruptedError("PDF OCR quotation was cancelled")
                    address = (source.key, page)
                    if address in accepted:
                        continue
                    text = page_text.get(address)
                    if source.mime.startswith("image/") and text is None:
                        text = ""
                    if ocr_policy != "all" and text is None:
                        native_path = directory / f"{page}.json"
                        payload = json.loads(native_path.read_text())
                        text = str(payload["text"])
                        native_files.setdefault(source.key, {})[page] = native_path
                    missing = text is None or not text.strip()
                    suspicious = bool(
                        text
                        and any(
                            str(getattr(flag, "name", flag)).upper() != "EMPTY"
                            for flag in assess_text_quality(text)
                        )
                    )
                    if (
                        ocr_policy == "all"
                        or missing
                        or (ocr_policy == "missing_or_suspicious" and suspicious)
                    ):
                        targets.append(
                            PdfSplitPageTarget(
                                source.key,
                                None,
                                "sha256:" + source.sha256.removeprefix("sha256:"),
                                source.source_column_id,
                                page,
                                params,
                            )
                        )
        quoted = (
            adapter.quote(
                targets=targets,
                plan_token=binding_hash,
                request_context=request_context,
            )
            if targets
            else {
                "pages": 0,
                "estimate": {
                    "rows": 0,
                    "cost": 0.0,
                    "billed_cost": 0,
                    "cost_source": "free_local",
                    "policy_id": "local",
                    "requires_confirmation": False,
                },
                "authorization": None,
            }
        )
        return PdfPacketOcrQuote(
            public={
                "pages": quoted["pages"],
                "estimate": quoted["estimate"],
                "binding_hash": binding_hash,
            },
            binding_hash=binding_hash,
            quoted=quoted,
            native_files=native_files,
        )

    def approve(
        self,
        holder: PdfPacketOcrQuote | None,
        *,
        project_id: str,
        sources: Sequence[AdmittedPacketSource],
        revision: int,
        ocr_policy: str,
        ocr_selection: Mapping[str, Any],
        confirmation: str | None,
        request_context: Any = None,
        current: PdfPacketOcrQuote | None = None,
    ) -> dict[str, Any] | None:
        params = packet_ocr_params(ocr_selection)
        binding_hash = _binding(sources, revision, ocr_policy, params)
        if holder is None:
            if ocr_policy == "none":
                return None
            raise ValueError("review the exact source-page OCR quote before applying")
        if holder.binding_hash != binding_hash:
            raise ValueError(
                "PDF source scope, OCR policy or engine changed; review the quote"
            )
        adapter = PdfSplitPreparationAdapter(
            self.workspace,
            preview_runs=self.preview_runs,
            inventory_path=self.inventory_path,
            project_id=project_id,
            admitted_sources={source.key: source for source in sources},
        )
        authorization = holder.quoted["authorization"]
        if current is None:
            adapter.approve(holder.quoted, confirmation=confirmation)
        else:
            from frisket.server.services.pdf_import_ocr_approval import (
                PdfImportOcrGrant,
            )
            from frisket.server.services.pdf_split_preparation import (
                PdfSplitPreparationStore,
            )

            if current.binding_hash != binding_hash:
                raise ValueError("PDF OCR source plan changed; review the quote")
            refreshed = current.quoted["authorization"]
            if refreshed is None:
                return None  # Every formerly paid target is now cached.
            if authorization is None:
                raise ValueError("PDF OCR work grew; review the current quote")
            approved = PdfImportOcrGrant.from_mapping(authorization)
            reduced = PdfImportOcrGrant.from_mapping(refreshed)
            children = {
                child["child_key"]: child for child in approved.allocation["children"]
            }
            if (
                approved.actor != reduced.actor
                or approved.route != reduced.route
                or approved.target != reduced.target
                or approved.ocr_action_params != reduced.ocr_action_params
                or any(
                    child != children.get(child["child_key"])
                    for child in reduced.allocation["children"]
                )
                or any(
                    target not in approved.scope["targets"]
                    for target in reduced.scope["targets"]
                )
            ):
                raise ValueError(
                    "PDF OCR work or route/price changed; review the current quote"
                )
            if (
                approved.allocation["requires_confirmation"]
                and confirmation != approved.promise_set_hash
            ):
                raise ValueError("PDF OCR requires confirmation of its exact claims")
            # Both holders are server-owned: the refreshed one must still be
            # the exact current quote, while the retained holder carries the
            # confirmed superset. Derive only the unchanged remaining children.
            with PdfSplitPreparationStore(self.inventory_path) as store:
                saved = store.db.execute(
                    "SELECT grant_json FROM pdf_split_page_quotes WHERE plan_token=?",
                    (binding_hash,),
                ).fetchone()
                encoded = json.dumps(
                    reduced.as_mapping(),
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                if saved is None or saved[0] != encoded:
                    raise ValueError("PDF OCR quote changed; review the current price")
                authorization = reduced.as_mapping()
                authorization["allocation"]["approval_parent_hash"] = (
                    approved.promise_set_hash
                )
                with store.db:
                    store.db.execute(
                        "INSERT OR REPLACE INTO pdf_split_page_grants VALUES(?,?)",
                        (
                            binding_hash,
                            json.dumps(
                                authorization,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ),
                        ),
                    )
        if authorization is None:
            return None
        return {
            "kind": "pdf_split_source_pages",
            "plan_token": binding_hash,
            "ocr_action_params": dict(params),
            "ocr_authorization": dict(authorization),
        }

    def estimate_sample(
        self,
        *,
        project_id: str,
        sources: Sequence[AdmittedPacketSource],
        source_key: str,
        page: int,
        revision: int,
        ocr_selection: Mapping[str, Any],
        force_rescan: bool = False,
        request_context: Any = None,
    ) -> dict[str, Any]:
        source = next((source for source in sources if source.key == source_key), None)
        if source is None or not source.page_start <= page <= source.page_end:
            raise ValueError("sample page is outside the admitted source")
        params = packet_ocr_params(ocr_selection)
        binding = _binding(sources, revision, "all", params)
        token = hashlib.sha256(
            json.dumps(
                {
                    "binding": binding,
                    "source": source_key,
                    "page": page,
                    "rescan": force_rescan,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        inventory_path = source.preview_inventory_path or self.inventory_path
        adapter = PdfSplitPreparationAdapter(
            self.workspace,
            preview_runs=self.preview_runs,
            inventory_path=inventory_path,
            project_id=project_id,
            admitted_sources={source.key: source},
        )
        target = PdfSplitPageTarget(
            source.key,
            None,
            "sha256:" + source.sha256.removeprefix("sha256:"),
            source.source_column_id,
            page,
            params,
        )
        quoted = adapter.quote(
            targets=[target],
            plan_token=token,
            request_context=request_context,
            force_rescan=force_rescan,
        )
        return {**quoted, "target": target, "plan_token": token}

    def start_sample(
        self,
        *,
        project_id: str,
        sources: Sequence[AdmittedPacketSource],
        source_key: str,
        page: int,
        revision: int,
        ocr_selection: Mapping[str, Any],
        confirmation: str | None,
        force_rescan: bool = False,
        request_context: Any = None,
    ) -> dict[str, Any]:
        import uuid

        source = next((source for source in sources if source.key == source_key), None)
        if source is None:
            raise ValueError("sample source is unavailable")
        prepared = self.estimate_sample(
            project_id=project_id,
            sources=sources,
            source_key=source_key,
            page=page,
            revision=revision,
            ocr_selection=ocr_selection,
            force_rescan=force_rescan,
            request_context=request_context,
        )
        inventory_path = source.preview_inventory_path or self.inventory_path
        adapter = PdfSplitPreparationAdapter(
            self.workspace,
            preview_runs=self.preview_runs,
            inventory_path=inventory_path,
            project_id=project_id,
            admitted_sources={source.key: source},
        )
        adapter.approve(prepared, confirmation=confirmation)
        target = prepared["target"]
        capture = None
        if source.source_row_id is not None:
            from frisket.engine.store.pdf_ocr_sample import capture_pdf_ocr_sample

            capture = capture_pdf_ocr_sample(
                self.workspace.get(project_id),
                sheet_id=int(source.source_sheet_id),
                row_id=source.source_row_id,
                source_column_id=int(source.source_column_id),
                blob_hash=source.sha256.removeprefix("sha256:"),
                filename=source.filename,
                page_count=source.page_count,
                page_number=page,
                expected_source_value=source.frozen_value,
                ocr_action_params=target.ocr_action_params,
            )
        if prepared["pages"] == 0:
            result = {}
            if capture is not None:
                from frisket.engine.store.pdf_ocr_sample import (
                    publish_cached_pdf_ocr_sample,
                )
                from frisket.server.services.pdf_split_preparation import (
                    PdfSplitPreparationStore,
                )

                with PdfSplitPreparationStore(inventory_path) as store:
                    cached_ref = store.cached_ref(target) or adapter.cached_reference(
                        target
                    )
                if cached_ref is None:
                    raise ValueError(
                        "cached OCR reference is unavailable; refresh setup"
                    )
                result = publish_cached_pdf_ocr_sample(
                    self.workspace.get(project_id),
                    capture,
                    prepared_ref_id=cached_ref,
                    project_id=project_id,
                )
            return {
                "jobs": (),
                "target": target,
                "generation": "",
                "operation_id": result.get("operation_id"),
                "cached": True,
                **result,
            }
        generation = "pdf-sample:" + uuid.uuid4().hex
        try:
            jobs = adapter.start_preview(
                targets=[target],
                plan_token=prepared["plan_token"],
                operation_id=0,
                generation=generation,
                confirmation=confirmation,
                request_context=request_context,
                force_rescan=force_rescan,
                original_publication=capture,
            )
        except BaseException:
            adapter.stop(generation=generation)
            raise
        if any("preview_id" not in job for job in jobs):
            adapter.stop(generation=generation)
            raise ValueError(
                "PDF OCR sample could not start; review the current engine and estimate"
            )
        return {
            "jobs": jobs,
            "target": target,
            "generation": generation,
            "operation_id": None,
            "cached": not bool(jobs),
        }
