"""Bundle-level I/O for a project store: streaming tar.gz export, raw SQLite
snapshot export, safe bundle import (member validation and streaming hash
verification), retained-history compaction, and bundle deletion. Free functions
over the facade's per-thread SQLite connection; ``project`` stays duck-typed
(``Any``) so this leaf never re-imports the facade module."""

from __future__ import annotations

import hashlib
import gzip
import io
import json
import os
import shutil
import sqlite3
import stat
import re
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from frisket.engine.store.disk_capacity import require_disk_headroom


class UnresolvedImportExportError(ValueError):
    """A bundle cannot preserve the external custody of an unfinished import."""


def _reject_unsafe_bundle_member(info: zipfile.ZipInfo, target_root: str) -> None:
    """Refuse a bundle zip member that is unsafe to extract.

    A member is rejected if its resolved destination would land outside
    ``target_root`` (an absolute path, a Windows drive letter, or a ``../``
    escape) or if it is a symlink (the Unix mode's S_ISLNK bit in
    ``external_attr``, the same check ``bundle_backup._reject_symlink`` applies
    to already-extracted files -- here it must run on the zip member itself,
    before that member is written into the unpublished staging directory).
    Extraction additionally allows only known bundle paths and uses exclusive
    creation to reject duplicate members.
    """
    name = info.filename
    if os.path.isabs(name) or name.startswith(("/", "\\")):
        raise ValueError(f"bundle member {name!r} has an unsafe absolute path")
    if len(name) >= 2 and name[1] == ":" and name[0].isalpha():
        raise ValueError(f"bundle member {name!r} has an unsafe absolute path")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ValueError(f"bundle member {name!r} is a symlink")
    mode = info.external_attr >> 16
    if stat.S_IFMT(mode) and not stat.S_ISREG(mode):
        raise ValueError("bundle members must be regular files")
    dest = os.path.realpath(os.path.join(target_root, name))
    if dest != target_root and not dest.startswith(target_root + os.sep):
        raise ValueError(f"bundle member {name!r} escapes the target directory")


def delete_bundle(path: str | Path) -> None:
    """Remove a bundle directory. The only truly destructive call; not an op."""
    p = Path(path)
    if (p / "manifest.json").exists():
        shutil.rmtree(p)


def export(
    project: Any,
    target_archive: str | Path,
    include_media: bool = True,
    *,
    include_traces: bool = False,
) -> Path:
    """Atomically publish a tar.gz bundle; stage its DB on the target disk."""
    target = Path(target_archive)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(fd)
    temp_target = Path(raw_temp)
    db = project.db
    if db.in_transaction:
        temp_target.unlink(missing_ok=True)
        raise RuntimeError("cannot export while this connection has pending writes")
    try:
        with tempfile.TemporaryDirectory(
            prefix=".frisket-snapshot-", dir=target.parent
        ) as temporary:
            snapshot_path = Path(temporary) / "project.db"
            snapshot = sqlite3.connect(snapshot_path)
            try:
                db.backup(snapshot, pages=256)
            finally:
                snapshot.close()
            snapshot = sqlite3.connect(snapshot_path)
            try:
                # Resumption also needs the external .imports inventory, which is
                # not part of a bundle. Never export an unrestorable guarded sheet.
                if snapshot.execute(
                    "SELECT 1 FROM import_sessions "
                    "WHERE state IN ('active','paused','cancelled') LIMIT 1"
                ).fetchone():
                    raise UnresolvedImportExportError(
                        "Finish imports, or cancel them and choose Keep "
                        "or Remove, before exporting a project bundle."
                    )
                with (
                    gzip.open(temp_target, "wb", compresslevel=1) as compressed,
                    tarfile.open(fileobj=compressed, mode="w|") as archive,
                ):
                    descriptor = json.dumps(
                        {
                            "format": "frisket-bundle",
                            "version": 1,
                            "include_media": include_media,
                            "include_traces": include_traces,
                        }
                    ).encode()
                    info = tarfile.TarInfo("bundle.json")
                    info.size = len(descriptor)
                    archive.addfile(info, io.BytesIO(descriptor))
                    archive.members.clear()
                    manifest = _project_manifest_bytes(
                        json.loads((project.path / "manifest.json").read_text())
                    )
                    info = tarfile.TarInfo("manifest.json")
                    info.size = len(manifest)
                    archive.addfile(info, io.BytesIO(manifest))
                    archive.members.clear()
                    _add_bundle_file(archive, snapshot_path, "project.db")
                    if include_media:
                        for (digest,) in snapshot.execute("SELECT hash FROM blobs"):
                            with project.materialize_blob(digest) as path:
                                _add_bundle_file(
                                    archive, Path(path), f"blobs/{digest[:2]}/{digest}"
                                )
                    if include_traces:
                        trace_dir = project.path / "traces"
                        if trace_dir.is_dir() and not trace_dir.is_symlink():
                            with os.scandir(trace_dir) as traces:
                                for trace in traces:
                                    if re.fullmatch(
                                        r"run-[A-Za-z0-9_.-]+\.jsonl\.gz", trace.name
                                    ) and trace.is_file(follow_symlinks=False):
                                        _add_bundle_file(
                                            archive,
                                            Path(trace.path),
                                            f"traces/{trace.name}",
                                        )
            finally:
                snapshot.close()
        os.replace(temp_target, target)
    finally:
        temp_target.unlink(missing_ok=True)
    return target


def _add_bundle_file(archive: tarfile.TarFile, path: Path, name: str) -> None:
    with path.open("rb") as source:
        info = tarfile.TarInfo(name)
        info.size = os.fstat(source.fileno()).st_size
        archive.addfile(info, source)
    # Python 3.12 retains member metadata even in streaming mode.
    archive.members.clear()


def _project_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    # Legacy ZIP exports mixed archive inventory into the project manifest.
    # It is not project state, and the captured DB now owns the blob inventory.
    cleaned = {
        key: value
        for key, value in manifest.items()
        if key not in {"blobs", "include_media", "include_traces"}
    }
    encoded = json.dumps(cleaned, indent=2).encode()
    if len(encoded) > 1024 * 1024:
        raise ValueError("bundle metadata exceeds 1 MiB")
    return encoded


def export_database(project: Any, target_db: str | Path) -> Path:
    """Write a consistent raw SQLite snapshot to a standalone db file."""
    if project.db.in_transaction:
        raise RuntimeError("cannot export while this connection has pending writes")
    target = Path(target_db)
    target.parent.mkdir(parents=True, exist_ok=True)
    project.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    dest = sqlite3.connect(target)
    try:
        project.db.backup(dest)
    finally:
        dest.close()
    return target


def compact(
    project: Any, vacuum: bool = True, *, force: bool = False
) -> dict[str, Any]:
    """Reclaim derived storage without deleting the project's journal.

    Authority history is durable even when its operation is discarded. Blob
    metadata GC therefore treats every stored cell/result/edit as a root, and
    this operation only removes metadata with no retained owner before an
    optional VACUUM returns already-free pages to the filesystem. The legacy
    ``results_pruned`` result field remains for API compatibility and is always
    zero.
    """
    if project.db.in_transaction:
        raise RuntimeError("cannot compact while this connection has pending writes")
    size_before = project.db_path.stat().st_size if project.db_path.exists() else 0
    if project.retention_policy()["no_compact"] and not force:
        return {
            "skipped": True,
            "reason": "project_no_compact",
            "results_pruned": 0,
            "blobs_removed": 0,
            "bytes_freed": 0,
            "db_bytes_before": size_before,
            "db_bytes_after": size_before,
            "db_bytes_reclaimed": 0,
        }
    blob_summary = project.gc_blobs()
    if vacuum:
        # checkpoint first so the VACUUM sees a clean main db
        project.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        project.db.execute("VACUUM")
        project.db.commit()
    size_after = project.db_path.stat().st_size if project.db_path.exists() else 0
    return {
        "skipped": False,
        "reason": None,
        "results_pruned": 0,
        "blobs_removed": blob_summary["blobs_removed"],
        "bytes_freed": blob_summary["bytes_freed"],
        "db_bytes_before": size_before,
        "db_bytes_after": size_after,
        "db_bytes_reclaimed": max(0, size_before - size_after),
    }


def _stamp_staged_manifest_for_target(staging: Path, target: Path) -> None:
    """Stamp path-derived manifest identity before publishing ``staging``.

    Project-open verification must run while the bundle is unpublished, and
    the staging directory must remain a direct sibling of ``target`` so the
    installation root (and therefore its consent principal) is the real
    workspace root. Opening that randomly named sibling stamps its temporary
    stem into ``manifest.json``; repair just that path-derived field through a
    unique sibling temp before the directory rename.
    """
    manifest_path = staging / "manifest.json"
    data = json.loads(manifest_path.read_text())
    if not isinstance(data, dict):
        raise ValueError("not a frisket bundle")
    data["project_id"] = target.stem
    fd, raw_temp = tempfile.mkstemp(
        dir=staging,
        prefix=".manifest.import-",
        suffix=".tmp",
    )
    scratch = Path(raw_temp)
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with handle:
            handle.write(json.dumps(data, indent=2))
        os.replace(scratch, manifest_path)
    finally:
        if fd >= 0:
            os.close(fd)
        scratch.unlink(missing_ok=True)


def import_bundle(
    project_cls: Any, source_archive: str | Path, target_dir: str | Path
) -> Any:
    target = Path(target_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{target.name}.import-",
            suffix=".tmp",
            dir=target.parent,
        )
    )
    try:
        if zipfile.is_zipfile(source_archive):
            with zipfile.ZipFile(source_archive) as archive:
                for info in archive.infolist():
                    _reject_unsafe_bundle_member(info, os.path.realpath(staging))
                    require_disk_headroom(staging, info.file_size)
                    with archive.open(info) as source:
                        _extract_bundle_file(staging, info.filename, source)
            descriptor = json.loads((staging / "manifest.json").read_text())
        else:
            with tarfile.open(source_archive, "r|gz") as archive:
                for info in archive:
                    if not info.isreg():
                        raise ValueError("bundle members must be regular files")
                    if (
                        info.name in {"bundle.json", "manifest.json"}
                        and info.size > 1024 * 1024
                    ):
                        raise ValueError("bundle metadata exceeds 1 MiB")
                    require_disk_headroom(staging, info.size)
                    with archive.extractfile(info) as source:
                        _extract_bundle_file(staging, info.name, source)
                    archive.members.clear()
            descriptor = json.loads((staging / "bundle.json").read_text())
            if (
                not isinstance(descriptor, dict)
                or type(descriptor.get("version")) is not int
                or descriptor["version"] != 1
            ):
                raise ValueError("unsupported bundle version")
            if any(
                type(descriptor.get(key)) is not bool
                for key in ("include_media", "include_traces")
            ):
                raise ValueError("invalid bundle inclusion flags")
        manifest = json.loads((staging / "manifest.json").read_text())
        if not isinstance(manifest, dict) or manifest.get("format") != "frisket-bundle":
            raise ValueError("not a frisket bundle")
        if (
            not isinstance(descriptor, dict)
            or descriptor.get("format") != "frisket-bundle"
        ):
            raise ValueError("not a frisket bundle")
        if type(descriptor.get("include_media", True)) is not bool:
            raise ValueError("invalid bundle media inclusion flag")
        (staging / "manifest.json").write_bytes(_project_manifest_bytes(manifest))
        # Integrity and the normal Project-open fences both run against the
        # unpublished staging tree. A malformed archive therefore never
        # becomes visible as target, even briefly.
        snapshot = sqlite3.connect(
            f"{(staging / 'project.db').resolve().as_uri()}?mode=ro", uri=True
        )
        try:
            for (digest,) in snapshot.execute("SELECT hash FROM blobs"):
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("invalid blob hash")
                path = staging / "blobs" / digest[:2] / digest
                if descriptor.get("include_media", True) and not path.is_file():
                    raise ValueError(f"bundle is missing required blob {digest}")
            (staging / "blobs").mkdir(exist_ok=True)
            with os.scandir(staging / "blobs") as prefixes:
                for prefix in prefixes:
                    with os.scandir(prefix.path) as blobs:
                        for blob in blobs:
                            if (
                                snapshot.execute(
                                    "SELECT 1 FROM blobs WHERE hash=?", (blob.name,)
                                ).fetchone()
                                is None
                            ):
                                raise ValueError("bundle contains an unregistered blob")
        finally:
            snapshot.close()
        (staging / "blobs").mkdir(exist_ok=True)
        staged_project = project_cls(staging)
        staged_project.close()
        _stamp_staged_manifest_for_target(staging, target)

        # This atomic filesystem publication is deliberately scoped to the
        # local library import seam. Hosted blob/CAS import needs a separate
        # staged-object publication protocol and remains future work.
        if target.exists():
            raise FileExistsError(f"{target} already exists")
        os.replace(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return project_cls(target)


def _extract_bundle_file(staging: Path, name: str, source: Any) -> None:
    blob = re.fullmatch(r"blobs/([0-9a-f]{2})/([0-9a-f]{64})", name)
    if blob and blob[1] != blob[2][:2]:
        raise ValueError("invalid blob path")
    if not (
        name in {"bundle.json", "manifest.json", "project.db"}
        or blob
        or re.fullmatch(r"traces/run-[A-Za-z0-9_.-]+\.jsonl\.gz", name)
    ):
        raise ValueError(f"unexpected bundle member {name!r}")
    path = staging / name
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    # Exclusive creation also rejects duplicate members without an in-memory set.
    with path.open("xb") as destination:
        while chunk := source.read(1024 * 1024):
            destination.write(chunk)
            if blob:
                digest.update(chunk)
    if blob and digest.hexdigest() != blob[2]:
        raise ValueError(f"blob {blob[2]} failed hash verification")
