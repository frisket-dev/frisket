"""Trusted packaged SQLite Vec1 loading and float32 vector boundaries."""

from __future__ import annotations

import math
import platform
import sqlite3
import struct
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


VEC1_VERSION = "0.8"
VECTOR_DIMENSIONS = 768
VECTOR_BYTES = VECTOR_DIMENSIONS * 4
VECTOR_NORM_TOLERANCE = 1e-3

RETRIEVAL_PAGE_SIZE = 16 * 1024
RETRIEVAL_CACHE_KIB = 32 * 1024
RETRIEVAL_MMAP_BYTES = 1024 * 1024 * 1024

_F32 = struct.Struct("<f")
_NATIVE_ROOT = Path(__file__).with_name("_native")


class Vec1Unavailable(RuntimeError):
    """The packaged Vec1 extension cannot be used by this SQLite runtime."""


@dataclass(frozen=True)
class RetrievalSQLiteSettings:
    cache_size: int
    mmap_size: int


def _platform_member() -> tuple[str, str]:
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        architecture = "x86_64"
    elif machine in {"arm64", "aarch64"}:
        architecture = "aarch64"
    else:
        raise Vec1Unavailable(f"unsupported Vec1 architecture: {machine or 'unknown'}")

    if sys.platform.startswith("linux"):
        return f"linux-{architecture}", "vec1.so"
    if sys.platform == "darwin" and architecture == "aarch64":
        return "macos-arm64", "vec1.dylib"
    if sys.platform == "win32" and architecture == "x86_64":
        return "windows-x86_64", "vec1.dll"
    raise Vec1Unavailable(f"unsupported Vec1 platform: {sys.platform}-{architecture}")


def packaged_vec1_path() -> Path:
    directory, filename = _platform_member()
    candidate = (_NATIVE_ROOT / directory / filename).resolve()
    try:
        candidate.relative_to(_NATIVE_ROOT.resolve())
    except ValueError as exc:
        raise Vec1Unavailable("packaged Vec1 path escaped its resource root") from exc
    if not candidate.is_file():
        raise Vec1Unavailable(
            f"Frisket has no packaged Vec1 library for {directory}; reinstall a "
            "supported platform wheel"
        )
    return candidate


def _loaded_version(connection: sqlite3.Connection) -> str | None:
    try:
        row = connection.execute("SELECT vec1_info()").fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None or not isinstance(row[0], str):
        return None
    return row[0]


def load_vec1(connection: sqlite3.Connection) -> str:
    """Load Frisket's packaged Vec1 once on ``connection``.

    SQLite extensions are connection-scoped. Loading is enabled only around the
    app-owned absolute resource path and is disabled again even when loading
    fails. A second call probes the existing connection and performs no load.
    """

    existing = _loaded_version(connection)
    if existing is not None:
        if not existing.startswith(f"version {VEC1_VERSION} "):
            raise Vec1Unavailable(f"unexpected SQLite Vec1 runtime: {existing}")
        return existing

    library = packaged_vec1_path()
    try:
        connection.enable_load_extension(True)
    except (AttributeError, sqlite3.Error) as exc:
        raise Vec1Unavailable(
            "this Python SQLite runtime does not support loadable extensions; "
            "install a supported Frisket runtime"
        ) from exc

    try:
        connection.load_extension(str(library))
    except sqlite3.Error as exc:
        raise Vec1Unavailable(
            f"could not load packaged SQLite Vec1 for SQLite {sqlite3.sqlite_version}: "
            f"{exc}"
        ) from exc
    finally:
        try:
            connection.enable_load_extension(False)
        except (AttributeError, sqlite3.Error) as exc:
            raise Vec1Unavailable(
                "SQLite Vec1 loaded but extension loading could not be disabled"
            ) from exc

    version = _loaded_version(connection)
    if version is None or not version.startswith(f"version {VEC1_VERSION} "):
        raise Vec1Unavailable(
            f"packaged SQLite Vec1 reported an unexpected runtime: {version!r}"
        )
    return version


def configure_retrieval_connection(
    connection: sqlite3.Connection,
) -> RetrievalSQLiteSettings:
    """Load Vec1 and apply the selected connection-local cache/mmap posture."""

    load_vec1(connection)
    connection.execute(f"PRAGMA cache_size=-{RETRIEVAL_CACHE_KIB}")
    connection.execute(f"PRAGMA mmap_size={RETRIEVAL_MMAP_BYTES}")
    cache_size = int(connection.execute("PRAGMA cache_size").fetchone()[0])
    mmap_row = connection.execute("PRAGMA mmap_size").fetchone()
    mmap_size = int(mmap_row[0]) if mmap_row is not None else 0
    return RetrievalSQLiteSettings(cache_size=cache_size, mmap_size=mmap_size)


def pack_vector(
    values: Iterable[float],
    *,
    dimensions: int = VECTOR_DIMENSIONS,
    norm_tolerance: float = VECTOR_NORM_TOLERANCE,
    require_unit_norm: bool = True,
) -> bytes:
    """Validate and encode one finite, unit-normalized little-endian f32 vector."""

    if dimensions <= 0:
        raise ValueError("vector dimensions must be positive")
    if not math.isfinite(norm_tolerance) or norm_tolerance < 0:
        raise ValueError("vector norm tolerance must be finite and non-negative")
    encoded = bytearray()
    squared_norm = 0.0
    count = 0
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("vector values must be finite")
        try:
            chunk = _F32.pack(number)
        except (OverflowError, struct.error) as exc:
            raise ValueError("vector values must fit finite float32") from exc
        stored = _F32.unpack(chunk)[0]
        if not math.isfinite(stored):
            raise ValueError("vector values must fit finite float32")
        encoded.extend(chunk)
        squared_norm += stored * stored
        count += 1
    if count != dimensions:
        raise ValueError(f"expected {dimensions} vector dimensions, got {count}")
    norm = math.sqrt(squared_norm)
    if require_unit_norm and abs(norm - 1.0) > norm_tolerance:
        raise ValueError(
            "vector must be unit-normalized: "
            f"norm {norm:.8g} is outside tolerance {norm_tolerance:.8g}"
        )
    return bytes(encoded)


def validate_vector_blob(
    blob: bytes,
    *,
    dimensions: int = VECTOR_DIMENSIONS,
    norm_tolerance: float = VECTOR_NORM_TOLERANCE,
    require_unit_norm: bool = True,
) -> bytes:
    """Validate an existing portable f32 BLOB with the same write contract."""

    if not isinstance(blob, bytes):
        raise TypeError("vector blob must be bytes")
    expected = dimensions * _F32.size
    if len(blob) != expected:
        raise ValueError(f"expected {expected} vector bytes, got {len(blob)}")
    return pack_vector(
        (value[0] for value in _F32.iter_unpack(blob)),
        dimensions=dimensions,
        norm_tolerance=norm_tolerance,
        require_unit_norm=require_unit_norm,
    )
