"""Bulk plan construction, publication, expiry, and claims."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from filelock import BaseFileLock, FileLock, Timeout

from frisket.server.route_errors import RouteError
from frisket.server.services import import_bulk_sources
from frisket.server.services.import_bulk_types import (
    BulkImportLimits,
    BulkUpload,
    ImportBulkRouteError,
)
from frisket.server.services.import_csv_analysis import inspect_csv_header_stream

TTL = 86_400
INVALID_CSV_HEADER = "CSV header is invalid"


class BulkPlanLifecycle:
    def __init__(self, clock):
        self.clock = clock

    async def create_plan(
        self,
        project: Any,
        project_id: str,
        *,
        uploads: list[BulkUpload],
        expand_archive: bool,
        limits: BulkImportLimits,
    ) -> dict[str, Any]:
        if expand_archive and (
            len(uploads) != 1
            or import_bulk_sources.source_suffix(uploads[0].logical_path) != ".zip"
        ):
            raise ImportBulkRouteError(
                400, "archive expansion requires one explicitly selected ZIP"
            )
        import_bulk_sources.enforce_limit(
            "upload file count", len(uploads), limits.max_upload_files
        )
        root = project.path / ".bulk_import_staging"
        root.mkdir(parents=True, exist_ok=True)
        pid = f"bulk-{secrets.token_urlsafe(18)}"
        draft = root / f".draft-{pid}"
        try:
            # Cleanup ownership starts before the first draft mutation, so a
            # failure creating either directory cannot strand half a draft.
            with lock(root / ".lock"):
                self.purge(root)
                draft.mkdir()
                (draft / "files").mkdir()
            staged = await import_bulk_sources.stage_uploads(
                uploads, draft, expand_archive, limits
            )
            outputs, questions = build_plan(staged, draft)
            now = self.clock()
            storage_identity = project.storage_identity
            atomic_json(
                draft / "manifest.json",
                {
                    "version": 1,
                    "plan_id": pid,
                    "project_id": project_id,
                    "storage_identity": storage_identity,
                    "published_at": now,
                    "expires_at": now + TTL,
                    "files": staged,
                    "outputs": outputs,
                    "questions": questions,
                },
            )
            with lock(root / ".lock"):
                self.purge(root)
                os.replace(draft, root / pid)
                import_bulk_sources.fsync_directory(root)
        except Exception:
            shutil.rmtree(draft, ignore_errors=True)
            import_bulk_sources.fsync_directory(root)
            raise
        return {
            "plan_id": pid,
            "questions": questions,
            "proposed_outputs": [public_output(o) for o in outputs],
            "message": f"Ready to import {len(staged)} files."
            if staged
            else "No importable files found.",
        }

    def claim_plan(
        self, project: Any, project_id: str, plan_id: str, decisions: dict[str, str]
    ) -> tuple[Path, Path, BaseFileLock, list[dict[str, Any]], list[dict[str, Any]]]:
        root = project.path / ".bulk_import_staging"
        if not safe_plan_id(plan_id):
            raise ImportBulkRouteError(404, "bulk import plan not found")
        if not root.is_dir():
            raise ImportBulkRouteError(404, "bulk import plan not found")
        plan = root / plan_id
        with lock(root / ".lock"):
            self.purge(root)
            manifest = self.manifest(
                plan / "manifest.json",
                plan_id,
                project_id,
                project.storage_identity,
            )
            questions, outputs, staged = (
                manifest.get("questions"),
                manifest.get("outputs"),
                manifest.get("files"),
            )
            if not all(isinstance(x, list) for x in (questions, outputs, staged)):
                raise ImportBulkRouteError(404, "bulk import plan not found")
            self.validate_decisions(questions, decisions)
            claim = open_lock_file(plan / ".claim.lock")
            claim.acquire()
            try:
                os.replace(plan / "manifest.json", plan / "claimed.json")
            except FileNotFoundError as exc:
                claim.release()
                raise ImportBulkRouteError(404, "bulk import plan not found") from exc
            # The rename is the ownership handoff. If acknowledging it fails,
            # clean it here; every later failure is covered by the facade's
            # import-work finally block.
            try:
                import_bulk_sources.fsync_directory(plan)
            except Exception:
                shutil.rmtree(plan, ignore_errors=True)
                import_bulk_sources.fsync_directory(root)
                claim.release()
                raise
        return root, plan, claim, outputs, staged

    def manifest(self, path: Path, pid: str, project_id: str, storage_identity: str):
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ImportBulkRouteError(404, "bulk import plan not found") from exc
        if (
            not isinstance(value, dict)
            or value.get("plan_id") != pid
            or value.get("project_id") != project_id
            or value.get("storage_identity") != storage_identity
            or not isinstance(value.get("expires_at"), (int, float))
            or value["expires_at"] <= self.clock()
        ):
            raise ImportBulkRouteError(404, "bulk import plan not found")
        return value

    def validate_decisions(self, questions, decisions):
        expected = {
            str(q["id"]): {"combine", "separate"}
            for q in questions
            if isinstance(q, dict) and q.get("kind") == "csv_combine"
        }
        if set(decisions) != set(expected) or any(
            v not in expected[k] for k, v in decisions.items()
        ):
            raise ImportBulkRouteError(
                400, "decisions must answer every plan question exactly"
            )

    def purge(self, root: Path):
        now = self.clock()
        for child in root.iterdir():
            try:
                child_mode = child.lstat().st_mode
            except OSError:
                continue
            if child.name == ".lock" or not stat.S_ISDIR(child_mode):
                continue
            if child.name.startswith(".draft-"):
                try:
                    stale = child.stat().st_mtime + TTL <= now
                except OSError:
                    stale = False
                if stale:
                    shutil.rmtree(child, ignore_errors=True)
                continue
            claim = child / ".claim.lock"
            lock_file = open_lock_file(claim)
            try:
                lock_file.acquire(timeout=0)
            except Timeout:
                continue
            try:
                manifest = child / (
                    "manifest.json"
                    if (child / "manifest.json").exists()
                    else "claimed.json"
                )
                try:
                    expired = (
                        float(json.loads(manifest.read_text())["expires_at"]) <= now
                    )
                except (OSError, ValueError, KeyError, TypeError):
                    expired = True
            finally:
                # Windows cannot remove a directory containing an open lock file.
                lock_file.release()
            if expired:
                shutil.rmtree(child, ignore_errors=True)


def build_plan(staged, root):
    groups = {}
    invalid_csvs = []
    for item in staged:
        if item["kind"] == "csv":
            source = import_bulk_sources.open_verified_source(root, item)
            try:
                headers = inspect_csv_header_stream(source)[0]
            except RouteError:
                invalid_csvs.append(item)
                continue
            finally:
                source.close()
            groups.setdefault(tuple(sorted(headers)), []).append(item)
    outputs = []
    questions = []
    for items in groups.values():
        paths = [x["logical_path"] for x in items]
        oid = stable_identifier("bulk-output", "csv-group", paths)
        output = {
            "id": oid,
            "kind": "csv_group",
            "sheet_name": stem(paths[0]),
            "logical_paths": paths,
        }
        if len(items) > 1:
            qid = stable_identifier("bulk-question", "csv-combine", paths)
            output["question_id"] = qid
            questions.append(
                {
                    "id": qid,
                    "kind": "csv_combine",
                    "default": "combine",
                    "logical_paths": paths,
                }
            )
        outputs.append(output)
    for item in invalid_csvs:
        path = item["logical_path"]
        outputs.append(
            {
                "id": stable_identifier("bulk-output", "csv-invalid", [path]),
                "kind": "csv_group",
                "sheet_name": stem(path),
                "logical_paths": [path],
                "planning_error": INVALID_CSV_HEADER,
            }
        )
    for item in staged:
        p = item["logical_path"]
        if item["kind"] == "xlsx":
            outputs.append(
                {
                    "id": stable_identifier("bulk-output", "xlsx", [p]),
                    "kind": "xlsx",
                    "sheet_name": stem(p),
                    "logical_paths": [p],
                }
            )
    for kind, outkind, name in (
        ("files", "files_group", "files"),
        ("email", "email", "Emails"),
    ):
        paths = [x["logical_path"] for x in staged if x["kind"] == kind]
        if paths:
            outputs.append(
                {
                    "id": stable_identifier("bulk-output", kind, paths),
                    "kind": outkind,
                    "sheet_name": name,
                    "logical_paths": paths,
                }
            )
    return sorted(outputs, key=lambda x: tuple(x["logical_paths"])), sorted(
        questions, key=lambda x: tuple(x["logical_paths"])
    )


def stem(path):
    return PurePosixPath(path).stem.strip() or "imported"


def stable_identifier(prefix, kind, paths):
    return f"{prefix}-{hashlib.sha256(json.dumps([kind, paths], separators=(',', ':')).encode()).hexdigest()[:16]}"


def public_output(o):
    return {k: o[k] for k in ("id", "kind", "sheet_name", "logical_paths")}


def atomic_json(path, value):
    temp = path.with_name(f".{path.name}-{secrets.token_urlsafe(6)}")
    with temp.open("w") as sink:
        json.dump(value, sink, sort_keys=True, separators=(",", ":"))
        sink.flush()
        os.fsync(sink.fileno())
    os.replace(temp, path)
    import_bulk_sources.fsync_directory(path.parent)


@contextmanager
def lock(path):
    with open_lock_file(path):
        yield


def open_lock_file(path: Path) -> BaseFileLock:
    return FileLock(str(path))


def safe_plan_id(value):
    return (
        value.startswith("bulk-")
        and 10 <= len(value) <= 64
        and all(x.isalnum() or x in "-_" for x in value)
    )


__all__ = [
    "BulkPlanLifecycle",
    "INVALID_CSV_HEADER",
    "TTL",
    "lock",
    "open_lock_file",
    "stable_identifier",
]
