"""Bounded admission and verified access for bulk staged sources."""

from __future__ import annotations
import hashlib
import lzma
import mimetypes
import os
import re
import secrets
import stat
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from frisket.server.services.import_bulk_types import (
    BulkImportLimits,
    BulkUpload,
    EmailFormat,
    ImportBulkRouteError,
    Kind,
)

CHUNK = 1024 * 1024
DETECTION_PREFIX_BYTES = 128 * 1024
_EMAIL_HEADER_RE = re.compile(rb"(?im)^(date|from|to|cc|subject|message-id):")


def enforce_limit(label, actual, limit):
    if limit is not None and actual > limit:
        raise ImportBulkRouteError(413, f"{label} exceeds deployment limit")


def normalize_logical_path(value):
    if not isinstance(value, str) or "\0" in value:
        raise ImportBulkRouteError(400, "invalid logical path")
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ImportBulkRouteError(400, "unsafe logical path")
    parts = [x for x in path.parts if x not in {"", "."}]
    if not parts or ":" in parts[0]:
        raise ImportBulkRouteError(400, "invalid logical path")
    return PurePosixPath(*parts).as_posix()


def is_junk_path(path):
    parts = PurePosixPath(path).parts
    name = parts[-1].casefold()
    return (
        any(x.casefold() == "__macosx" for x in parts)
        or name in {".ds_store", "thumbs.db", "desktop.ini"}
        or name.startswith("._")
        or name.startswith("~$")
        or (name.startswith(".~lock.") and name.endswith("#"))
    )


def source_suffix(path):
    return PurePosixPath(path).suffix.casefold()


def extend_detection_prefix(prefix: bytearray, chunk: bytes) -> None:
    remaining = DETECTION_PREFIX_BYTES - len(prefix)
    if remaining > 0:
        prefix.extend(chunk[:remaining])


def recognizable_email_headers(payload: bytes) -> set[bytes]:
    ends = [
        position
        for marker in (b"\r\n\r\n", b"\n\n")
        if (position := payload.find(marker)) >= 0
    ]
    if not ends or b"\x00" in payload:
        return set()
    header_block = payload[: min(ends)]
    return {match.group(1).lower() for match in _EMAIL_HEADER_RE.finditer(header_block)}


def detected_email_format(path: str, prefix: bytes) -> EmailFormat | None:
    suffix = source_suffix(path)
    if suffix == ".eml":
        return "eml"
    if suffix == ".mbox":
        return "mbox"

    parent = PurePosixPath(path).parent.name.casefold()
    if parent in {"cur", "new"} and len(recognizable_email_headers(prefix)) >= 2:
        return "eml"

    if suffix:
        return None
    separator_end = prefix.find(b"\n")
    if separator_end < 0 or not prefix[:separator_end].rstrip(b"\r").startswith(
        b"From "
    ):
        return None
    if recognizable_email_headers(prefix[separator_end + 1 :]):
        return "mbox"
    return None


def classify_source(path: str, prefix: bytes) -> tuple[Kind, EmailFormat | None]:
    suffix = source_suffix(path)
    email_format = detected_email_format(path, prefix)
    if email_format is not None:
        return "email", email_format
    if suffix == ".csv":
        return "csv", None
    if suffix == ".xlsx":
        return "xlsx", None
    return "files", None


def staged_item(path, mime, digest, size, *, prefix: bytes = b""):
    kind, email_format = classify_source(path, prefix)
    item = {
        "logical_path": path,
        "filename": PurePosixPath(path).name,
        "mime": mime,
        "kind": kind,
        "path": f"files/{digest}",
        "sha256": digest,
        "size": size,
    }
    if email_format is not None:
        item["email_format"] = email_format
    return item


def open_verified_source(root: Path, item: dict[str, Any]) -> BinaryIO:
    digest = item.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or digest != digest.lower()
        or any(character not in "0123456789abcdef" for character in digest)
        or item.get("path") != f"files/{digest}"
    ):
        raise ImportBulkRouteError(404, "bulk import plan not found")
    source_path = root / "files" / digest
    try:
        expected_metadata = None
        if os.open in os.supports_dir_fd:
            directory = os.open(
                root / "files",
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                descriptor = os.open(
                    digest,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory,
                )
            finally:
                os.close(directory)
        else:
            # Windows has no dir_fd-relative os.open. Refuse links/reparse
            # points, then prove the inspected path and opened handle identify
            # the same file before trusting its contents.
            expected_metadata = os.lstat(source_path)
            reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if (
                not stat.S_ISREG(expected_metadata.st_mode)
                or getattr(expected_metadata, "st_file_attributes", 0) & reparse_point
            ):
                raise OSError("staged source is not a regular file")
            descriptor = os.open(
                source_path,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        source = os.fdopen(descriptor, "rb")
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or (
            expected_metadata is not None
            and not os.path.samestat(expected_metadata, metadata)
        ):
            raise OSError("staged source is not a regular file")
        actual_digest, actual_size = hash_stream(source)
        if actual_digest != digest or actual_size != item.get("size"):
            raise OSError("staged source fingerprint changed")
        source.seek(0)
        return source
    except OSError as exc:
        if "source" in locals():
            source.close()
        raise ImportBulkRouteError(404, "bulk import plan not found") from exc


def hash_stream(source: BinaryIO):
    digest = hashlib.sha256()
    size = 0
    source.seek(0)
    while chunk := source.read(CHUNK):
        size += len(chunk)
        digest.update(chunk)
    source.seek(0)
    return digest.hexdigest(), size


def publish_staged_file(temp, destination):
    if destination.exists():
        if not stat.S_ISREG(destination.lstat().st_mode):
            raise ImportBulkRouteError(400, "unsafe staging destination")
        temp.unlink()
    else:
        os.replace(temp, destination)
    fsync_directory(destination.parent)


def fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


async def stage_uploads(
    uploads: list[BulkUpload], draft: Path, expand: bool, limits: BulkImportLimits
) -> list[dict[str, Any]]:
    staged = []
    seen = set()
    total_size = 0
    for i, u in enumerate(uploads):
        logical = normalize_logical_path(u.logical_path)
        if logical in seen:
            raise ImportBulkRouteError(
                400, "bulk import contains duplicate logical paths"
            )
        seen.add(logical)
        temp = draft / f".upload-{i}-{secrets.token_urlsafe(6)}"
        digest = hashlib.sha256()
        size = 0
        prefix = bytearray()
        try:
            with temp.open("wb") as sink:
                while chunk := await u.file.read(CHUNK):
                    size += len(chunk)
                    total_size += len(chunk)
                    enforce_limit("upload bytes", total_size, limits.max_upload_bytes)
                    digest.update(chunk)
                    extend_detection_prefix(prefix, chunk)
                    sink.write(chunk)
                sink.flush()
                os.fsync(sink.fileno())
            if expand:
                staged += stage_zip(temp, draft / "files", limits)
            elif not is_junk_path(logical):
                publish_staged_file(temp, draft / "files" / digest.hexdigest())
                staged.append(
                    staged_item(
                        logical,
                        u.mime,
                        digest.hexdigest(),
                        size,
                        prefix=bytes(prefix),
                    )
                )
        finally:
            temp.unlink(missing_ok=True)
    paths = [x["logical_path"] for x in staged]
    if len(paths) != len(set(paths)):
        raise ImportBulkRouteError(400, "bulk import contains duplicate logical paths")
    return sorted(staged, key=lambda x: x["logical_path"])


def stage_zip(
    path: Path, files: Path, limits: BulkImportLimits
) -> list[dict[str, Any]]:
    staged = []
    seen = set()
    expanded = 0
    try:
        with zipfile.ZipFile(path) as archive:
            infos = sorted(archive.infolist(), key=lambda x: x.filename)
            enforce_limit(
                "archive member count", len(infos), limits.max_archive_members
            )
            for info in infos:
                # ZipInfo.filename is truncated at the first NUL by the
                # standard library. Validate the original member name so
                # malformed archives cannot silently change logical paths.
                logical = normalize_logical_path(info.orig_filename)
                if logical in seen:
                    raise ImportBulkRouteError(
                        400, "ZIP archive contains duplicate normalized paths"
                    )
                seen.add(logical)
                if info.flag_bits & 1:
                    raise ImportBulkRouteError(
                        400, "encrypted ZIP archives are unsupported"
                    )
                mode = info.external_attr >> 16
                if info.is_dir():
                    continue
                if info.create_system == 3 and stat.S_IFMT(mode) not in {
                    0,
                    stat.S_IFREG,
                }:
                    raise ImportBulkRouteError(
                        400, "ZIP archive contains a non-regular file"
                    )
                if is_junk_path(logical):
                    continue
                temp = files / f".member-{secrets.token_urlsafe(6)}"
                digest = hashlib.sha256()
                size = 0
                prefix = bytearray()
                try:
                    with archive.open(info) as source, temp.open("wb") as sink:
                        while True:
                            try:
                                chunk = source.read(CHUNK)
                            except OSError as exc:
                                if (
                                    info.compress_type != zipfile.ZIP_BZIP2
                                    or exc.errno is not None
                                ):
                                    raise
                                # The BZIP2 decoder reports malformed
                                # compressed data as a plain OSError.
                                raise ImportBulkRouteError(
                                    400, "invalid ZIP archive"
                                ) from exc
                            if not chunk:
                                break
                            size += len(chunk)
                            expanded += len(chunk)
                            enforce_limit(
                                "expanded archive bytes",
                                expanded,
                                limits.max_expanded_bytes,
                            )
                            digest.update(chunk)
                            extend_detection_prefix(prefix, chunk)
                            sink.write(chunk)
                        sink.flush()
                        os.fsync(sink.fileno())
                    publish_staged_file(temp, files / digest.hexdigest())
                finally:
                    temp.unlink(missing_ok=True)
                staged.append(
                    staged_item(
                        logical,
                        mimetypes.guess_type(logical)[0] or "application/octet-stream",
                        digest.hexdigest(),
                        size,
                        prefix=bytes(prefix),
                    )
                )
    except ImportBulkRouteError:
        raise
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        UnicodeError,
        RuntimeError,
        EOFError,
        lzma.LZMAError,
        zlib.error,
    ) as exc:
        raise ImportBulkRouteError(400, "invalid ZIP archive") from exc
    return staged
