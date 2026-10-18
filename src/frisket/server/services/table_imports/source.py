"""Immutable source custody and transient workbook decryption."""

from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path
from typing import Any, Iterator

from frisket.server.services.import_uploads import AdmittedUpload


COPY_CHUNK_BYTES = 1024 * 1024


class InvalidWorkbookPassword(ValueError):
    pass


def stage_upload(upload: AdmittedUpload, target: Path) -> None:
    with _upload_source(upload) as source, target.open("xb") as sink:
        digest = hashlib.sha256()
        size = 0
        while chunk := source.read(COPY_CHUNK_BYTES):
            sink.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        sink.flush()
        os.fsync(sink.fileno())
    if size != upload.size or digest.hexdigest() != upload.sha256:
        raise ValueError("uploaded source changed during admission")


@contextlib.contextmanager
def _upload_source(upload: AdmittedUpload) -> Iterator[Any]:
    if upload.open_source is not None:
        with upload.open_source() as source:
            yield source
        return
    assert upload.source is not None
    upload.source.seek(0)
    try:
        yield upload.source
    finally:
        upload.source.seek(0)


def known_unsupported(path: Path) -> bool:
    with path.open("rb") as source:
        prefix = source.read(16)
    return prefix.startswith(
        (b"%PDF-", b"\x89PNG", b"GIF87a", b"GIF89a", b"\xff\xd8\xff")
    )


def office_encrypted(path: Path) -> bool:
    try:
        import msoffcrypto

        with path.open("rb") as source:
            office = msoffcrypto.OfficeFile(source)
            return bool(office.is_encrypted())
    except Exception:
        return False


def decrypt_office(source_path: Path, output_path: Path, password: str) -> None:
    temporary = output_path.with_name(".decrypted.tmp")
    temporary.unlink(missing_ok=True)
    try:
        import msoffcrypto

        with source_path.open("rb") as source, temporary.open("xb") as sink:
            office = msoffcrypto.OfficeFile(source)
            office.load_key(password=password, verify_password=True)
            office.decrypt(sink)
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary, output_path)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise InvalidWorkbookPassword from exc


def adopt_decrypted_source(directory: Path) -> tuple[int, str]:
    source = directory / "source"
    decrypted = directory / "decrypted"
    os.replace(source, directory / "encrypted-source")
    os.replace(decrypted, source)
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        while chunk := stream.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
    return source.stat().st_size, digest.hexdigest()


__all__ = [
    "InvalidWorkbookPassword",
    "adopt_decrypted_source",
    "decrypt_office",
    "known_unsupported",
    "office_encrypted",
    "stage_upload",
]
