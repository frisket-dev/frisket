"""Stateful OCR preview orchestration for a PDF import draft."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from filelock import FileLock

from .inventory import PdfDraftInventory
from .previews import prepare_ocr, preview_settings
from .storage import PdfImportDraftError, SCHEMA_VERSION


class PreviewHost(Protocol):
    _preview_runs: Any

    def _open(self, project_id: str, draft_id: str) -> tuple[Path, dict[str, Any]]: ...

    def _lock(self, directory: Path) -> FileLock: ...

    def _header(self, project_id: str, directory: Path) -> dict[str, Any]: ...

    def _save(self, directory: Path, header: dict[str, Any]) -> None: ...

    def _files_root(self, directory: Path, header: Mapping[str, Any]) -> Path: ...

    def _require_state(self, header: Mapping[str, Any], state: str) -> None: ...

    def _staged(
        self, inventory: PdfDraftInventory, occurrence_id: str
    ) -> dict[str, Any]: ...

    def _ocr_selection(self, value: Mapping[str, Any]) -> dict[str, Any]: ...

    def _ocr_action_params(
        self, selection: Mapping[str, Any]
    ) -> dict[str, Any]: ...


def estimate(
    host: PreviewHost,
    project_id: str,
    draft_id: str,
    occurrence_id: str,
    page: int,
    *,
    ocr_selection: Mapping[str, Any],
    request_context: Any = None,
) -> dict[str, Any]:
    action_params = host._ocr_action_params(host._ocr_selection(ocr_selection))
    prepared = _prepare(
        host,
        project_id,
        draft_id,
        occurrence_id,
        page,
        action_params,
        request_context=request_context,
    )
    estimate_value = (
        None
        if prepared["cached"]
        else host._preview_runs.scratch_estimate(project_id, prepared["plan"])
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "draft_id": draft_id,
        "occurrence_id": occurrence_id,
        "page": page,
        "ocr_action_params": action_params,
        "cached": prepared["cached"] is not None,
        "estimate": estimate_value,
    }


def start(
    host: PreviewHost,
    project_id: str,
    draft_id: str,
    occurrence_id: str,
    page: int,
    *,
    ocr_selection: Mapping[str, Any],
    confirmation: str | None,
    request_context: Any = None,
) -> dict[str, Any]:
    action_params = host._ocr_action_params(host._ocr_selection(ocr_selection))
    prepared = _prepare(
        host,
        project_id,
        draft_id,
        occurrence_id,
        page,
        action_params,
        request_context=request_context,
    )
    cached = prepared["cached"]
    if cached:
        return {
            "schema_version": SCHEMA_VERSION,
            "draft_id": draft_id,
            "occurrence_id": occurrence_id,
            "page": page,
            "ocr_action_params": action_params,
            "job_id": None,
            "status": "done",
            "receipt_id": cached.get("receipt_id"),
            "estimate": None,
            "needs_confirmation": False,
        }
    plan = prepared["plan"]
    estimate_value = host._preview_runs.scratch_estimate(project_id, plan)
    started = host._preview_runs.start_scratch_preview(
        project_id,
        plan,
        confirmation=confirmation,
        router=prepared["router"],
        composition=prepared["composition"],
        execution_context=prepared["execution_context"],
        replacement_key=(
            f"pdf-import:{draft_id}:{occurrence_id}:{page}:"
            f"{prepared['settings_hash']}"
        ),
        total=1,
    )
    if started.status_code != 202:
        raise PdfImportDraftError(started.status_code, started.payload, bare_json=True)
    payload = dict(started.payload)
    directory, _ = host._open(project_id, draft_id)
    with host._lock(directory):
        header = host._header(project_id, directory)
        lost_state = str(header["state"]) if header["state"] != "open" else None
        if lost_state is None:
            header["preview_jobs"] = [
                value
                for value in header["preview_jobs"]
                if not (
                    value["occurrence_id"] == occurrence_id
                    and value["page"] == page
                    and value["ocr_action_params"] == action_params
                )
            ][-31:]
            header["preview_jobs"].append(
                {
                    "job_id": str(payload["preview_id"]),
                    "occurrence_id": occurrence_id,
                    "page": page,
                    "ocr_action_params": action_params,
                    "status": "running",
                    "receipt_id": payload.get("receipt_id"),
                    "error": None,
                }
            )
            host._save(directory, header)
    if lost_state is not None:
        host._preview_runs.cancel_preview(project_id, str(payload["preview_id"]))
        raise PdfImportDraftError(409, f"PDF import is {lost_state}")
    return {
        "schema_version": SCHEMA_VERSION,
        "draft_id": draft_id,
        "occurrence_id": occurrence_id,
        "page": page,
        "ocr_action_params": action_params,
        "job_id": str(payload["preview_id"]),
        "status": "running",
        "receipt_id": payload.get("receipt_id"),
        "estimate": estimate_value,
        "needs_confirmation": False,
    }


def refresh(host: PreviewHost, project_id: str, header: dict[str, Any]) -> bool:
    if host._preview_runs is None:
        return False
    changed = False
    for job in header["preview_jobs"]:
        if job["status"] != "running":
            continue
        response = host._preview_runs.get_preview(project_id, str(job["job_id"]))
        payload: Mapping[str, Any]
        if response.status_code == 404:
            payload = {}
            status = "failed"
            error = "OCR preview expired before its result was collected"
        elif response.status_code == 200:
            payload = response.payload
            raw_status = payload.get("status")
            status = "failed" if raw_status == "error" else str(raw_status)
            if status not in {"running", "done", "failed", "cancelled"}:
                continue
            raw_error = payload.get("error")
            error = (
                str(raw_error.get("message") or raw_error)[:500]
                if isinstance(raw_error, Mapping)
                else (str(raw_error)[:500] if raw_error else None)
            )
        else:
            continue
        accounting = payload.get("accounting")
        receipt_id = (
            accounting.get("receipt_id") if isinstance(accounting, Mapping) else None
        )
        if (
            job["status"] != status
            or job.get("error") != error
            or (receipt_id is not None and job.get("receipt_id") != receipt_id)
        ):
            job["status"] = status
            job["error"] = error
            if receipt_id is not None:
                job["receipt_id"] = str(receipt_id)
            changed = True
    return changed


def _prepare(
    host: PreviewHost,
    project_id: str,
    draft_id: str,
    occurrence_id: str,
    page: int,
    action_params: Mapping[str, Any],
    *,
    request_context: Any,
) -> dict[str, Any]:
    if host._preview_runs is None:
        raise PdfImportDraftError(503, "OCR preview is unavailable")
    directory, header = host._open(project_id, draft_id)
    with host._lock(directory):
        header = host._header(project_id, directory)
        host._require_state(header, "open")
    engine = str(action_params["engine"])
    with PdfDraftInventory(directory / "inventory.db") as inventory:
        item = host._staged(inventory, occurrence_id)
        _settings, settings_hash = preview_settings(action_params)
        cached = inventory.cached_ocr(
            str(item["sha256"]), page, engine, settings_hash
        )
    if cached is not None:
        return {"cached": cached, "settings_hash": settings_hash}
    prepared = prepare_ocr(
        host._preview_runs,
        project_id=project_id,
        source=host._files_root(directory, header) / str(item["sha256"]),
        item=item,
        occurrence_id=occurrence_id,
        page=page,
        action_params=action_params,
        request_context=request_context,
        on_page=lambda value: _cache(
            host,
            project_id,
            draft_id,
            str(item["sha256"]),
            engine,
            settings_hash,
            action_params,
            value,
        ),
    )
    return {"cached": None, **prepared}


def _cache(
    host: PreviewHost,
    project_id: str,
    draft_id: str,
    sha256: str,
    engine: str,
    settings_hash: str,
    action_params: Mapping[str, Any],
    page: Mapping[str, Any],
) -> None:
    number = page.get("page")
    if type(number) is not int:
        return
    directory, _ = host._open(project_id, draft_id)
    with (
        host._lock(directory),
        PdfDraftInventory(directory / "inventory.db") as inventory,
    ):
        header = host._header(project_id, directory)
        if header["state"] in {"cancelling", "cancelled"}:
            return
        inventory.cache_ocr(
            sha256,
            number,
            engine,
            settings_hash,
            {
                "engine": engine,
                "ocr_action_params": dict(action_params),
                "text": str(page.get("text") or ""),
                "blocks": list(page.get("blocks") or []),
                "width": page.get("width"),
                "height": page.get("height"),
                "receipt_id": page.get("receipt_id"),
            },
        )


__all__ = ["PreviewHost", "estimate", "refresh", "start"]
