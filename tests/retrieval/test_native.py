from __future__ import annotations

import hashlib
import math
import platform
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
from pathlib import Path
from runpy import run_path
from types import ModuleType

import pytest

from frisket.retrieval import native


ROOT = Path(__file__).resolve().parents[2]
VENDORED_SOURCE = {
    "vec1.c": (
        371445,
        "4f9a83efd78992769ba7a8e630d755b93370258387694498f22d1eb3cfd3e766",
    ),
    "sqlite3.h": (
        695737,
        "cd20a2877bc82b94e36c1093b727dd87cfa3c1d581aac6e240925535cbd90166",
    ),
    "sqlite3ext.h": (
        39547,
        "48963abfefb41ac329ef9d706acb5eadba952a99a748285c9ded6b5e0942721b",
    ),
}


@pytest.fixture(scope="session")
def built_native_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    source_root = _native_build_source(tmp_path_factory.mktemp("vec1-source"))
    with pytest.MonkeyPatch.context() as monkeypatch:
        hook = _editable_hook(monkeypatch, source_root)
        hook.initialize("editable", {})
        hook.finalize("editable", {}, "unused.whl")
    return source_root / "src" / "frisket" / "retrieval" / "_native"


@pytest.fixture
def packaged_native(monkeypatch: pytest.MonkeyPatch, built_native_root: Path) -> Path:
    monkeypatch.setattr(native, "_NATIVE_ROOT", built_native_root)
    return native.packaged_vec1_path()


def test_vendored_source_matches_reviewed_sqlite_release() -> None:
    source_root = ROOT / "vendor" / "sqlite-vec1"
    for filename, (size, digest) in VENDORED_SOURCE.items():
        source = source_root / filename
        assert source.stat().st_size == size
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest


def _load_build_hook(monkeypatch: pytest.MonkeyPatch):
    interface = ModuleType("hatchling.builders.hooks.plugin.interface")
    interface.BuildHookInterface = type("BuildHookInterface", (), {})
    for name in (
        "hatchling",
        "hatchling.builders",
        "hatchling.builders.hooks",
        "hatchling.builders.hooks.plugin",
    ):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    monkeypatch.setitem(sys.modules, interface.__name__, interface)
    return run_path(str(ROOT / "hatch_build.py"))


def _load_wheel_platform_tag(monkeypatch: pytest.MonkeyPatch):
    return _load_build_hook(monkeypatch)["_wheel_platform_tag"]


def _native_build_source(source_root: Path) -> Path:
    (source_root / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / "build_vec1.py", source_root / "scripts")
    shutil.copytree(
        ROOT / "vendor" / "sqlite-vec1", source_root / "vendor" / "sqlite-vec1"
    )
    return source_root


def _editable_hook(monkeypatch: pytest.MonkeyPatch, source_root: Path):
    hook = _load_build_hook(monkeypatch)["CustomBuildHook"]()
    hook.target_name = "wheel"
    hook.root = str(source_root)
    hook.directory = str(source_root / "dist")
    return hook


def test_editable_build_keeps_loadable_runtime_beside_source_after_finalize(
    packaged_native: Path,
) -> None:
    assert packaged_native.parents[1].parts[-4:] == (
        "src",
        "frisket",
        "retrieval",
        "_native",
    )
    with sqlite3.connect(":memory:") as connection:
        assert native.load_vec1(connection).startswith("version 0.8 ")
        assert connection.execute(
            "SELECT vec1_cos_distance(?, ?)",
            (native.pack_vector([1.0, 0.0], dimensions=2),) * 2,
        ).fetchone()[0] == pytest.approx(0.0)


def test_editable_build_refuses_missing_compiler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    source_root = _native_build_source(tmp_path)
    hook = _editable_hook(monkeypatch, source_root)
    monkeypatch.setenv("CC", str(tmp_path / "no-such-compiler"))

    with pytest.raises(subprocess.CalledProcessError):
        hook.initialize("editable", {})

    assert "native compiler not found" in capfd.readouterr().err
    assert not list(source_root.rglob("vec1.so"))
    assert not list(source_root.rglob("vec1.dylib"))
    assert not list(source_root.rglob("vec1.dll"))


def test_wheel_platform_defaults_to_generic_host_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FRISKET_WHEEL_PLATFORM_TAG", raising=False)
    wheel_platform_tag = _load_wheel_platform_tag(monkeypatch)
    expected = (
        "macosx_14_0_arm64"
        if sys.platform == "darwin"
        else sysconfig.get_platform().replace("-", "_").replace(".", "_")
    )
    assert wheel_platform_tag() == expected
    if sys.platform.startswith("linux"):
        assert not wheel_platform_tag().startswith("manylinux_")


def test_macos_wheel_tag_matches_vec1_compiler_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FRISKET_WHEEL_PLATFORM_TAG", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    wheel_platform_tag = _load_wheel_platform_tag(monkeypatch)

    build = run_path(str(ROOT / "scripts" / "build_vec1.py"))
    commands: list[list[str]] = []
    compile_unix = build["_compile_unix"]
    compile_unix.__globals__["_run"] = commands.append
    compile_unix(
        ROOT / "vendor" / "sqlite-vec1" / "vec1.c",
        tmp_path / "vec1.dylib",
        tmp_path,
        macos=True,
    )
    compile_command = next(command for command in commands if "-c" in command)
    architecture = compile_command[compile_command.index("-arch") + 1]
    deployment = next(
        value.removeprefix("-mmacosx-version-min=")
        for value in compile_command
        if value.startswith("-mmacosx-version-min=")
    )
    expected = f"macosx_{deployment.replace('.', '_')}_{architecture}"
    assert wheel_platform_tag() == expected == "macosx_14_0_arm64"


def test_windows_builder_uses_linker_beside_selected_msvc_compiler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    build = run_path(str(ROOT / "scripts" / "build_vec1.py"))
    compiler = tmp_path / "msvc" / "bin" / "cl.exe"
    commands: list[list[str]] = []
    compile_windows = build["_compile_windows"]
    monkeypatch.setattr(build["shutil"], "which", lambda _name: str(compiler))
    compile_windows.__globals__["_run"] = commands.append

    compile_windows(
        ROOT / "vendor" / "sqlite-vec1" / "vec1.c",
        tmp_path / "vec1.dll",
        tmp_path,
    )

    assert commands[0][0] == str(compiler)
    assert commands[1][0] == str(compiler)
    assert commands[2][0] == str(compiler.with_name("link.exe"))


def test_wheel_platform_reserves_override_for_matching_manylinux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel_platform_tag = _load_wheel_platform_tag(monkeypatch)
    host = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    machine = "x86_64" if host.endswith(("x86_64", "amd64")) else "aarch64"
    override = f"manylinux_2_34_{machine}"
    monkeypatch.setenv("FRISKET_WHEEL_PLATFORM_TAG", override)
    if sys.platform.startswith("linux"):
        assert wheel_platform_tag() == override
    else:
        with pytest.raises(RuntimeError, match="does not match build host"):
            wheel_platform_tag()
    monkeypatch.setenv("FRISKET_WHEEL_PLATFORM_TAG", f"linux_{machine}")
    with pytest.raises(RuntimeError, match="reserved for verified manylinux"):
        wheel_platform_tag()


@pytest.mark.usefixtures("packaged_native")
def test_loader_is_idempotent_and_disables_extension_loading(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "load.db") as connection:
        first = native.load_vec1(connection)
        second = native.load_vec1(connection)
        assert first == second
        assert first.startswith("version 0.8 ")
        with pytest.raises(sqlite3.OperationalError, match="not authorized|disabled"):
            connection.execute(
                "SELECT load_extension(?)", (str(native.packaged_vec1_path()),)
            )


def test_loader_disables_extension_loading_after_load_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    packaged_native: Path,
) -> None:
    bad_root = tmp_path / "native"
    directory, filename = native._platform_member()
    bad_library = bad_root / directory / filename
    bad_library.parent.mkdir(parents=True)
    bad_library.write_bytes(b"not a shared library")
    monkeypatch.setattr(native, "_NATIVE_ROOT", bad_root)
    with sqlite3.connect(tmp_path / "failure.db") as connection:
        with pytest.raises(native.Vec1Unavailable, match="could not load"):
            native.load_vec1(connection)
        with pytest.raises(sqlite3.OperationalError, match="not authorized|disabled"):
            connection.execute("SELECT load_extension(?)", (str(packaged_native),))


@pytest.mark.usefixtures("packaged_native")
def test_configure_in_memory_connection_reports_unavailable_mmap() -> None:
    with sqlite3.connect(":memory:") as connection:
        settings = native.configure_retrieval_connection(connection)

    assert settings.cache_size == -native.RETRIEVAL_CACHE_KIB
    assert settings.mmap_size == 0


@pytest.mark.usefixtures("packaged_native")
def test_native_relation_first_query_fts_and_reopen(tmp_path: Path) -> None:
    database = tmp_path / "retrieval.db"
    query = native.pack_vector([1.0, 0.0, 0.0], dimensions=3)
    near = native.pack_vector([1.0, 0.0, 0.0], dimensions=3)
    middle = native.pack_vector([0.8, 0.6, 0.0], dimensions=3)
    far = native.pack_vector([0.0, 1.0, 0.0], dimensions=3)

    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA page_size={native.RETRIEVAL_PAGE_SIZE}")
        connection.executescript(
            """
            CREATE TABLE vectors(part_id INTEGER PRIMARY KEY, embedding BLOB NOT NULL);
            CREATE TABLE eligible(part_id INTEGER PRIMARY KEY);
            CREATE VIRTUAL TABLE captions USING fts5(part_id UNINDEXED, text);
            """
        )
        settings = native.configure_retrieval_connection(connection)
        assert settings.cache_size == -native.RETRIEVAL_CACHE_KIB
        assert settings.mmap_size == native.RETRIEVAL_MMAP_BYTES
        assert connection.execute("PRAGMA page_size").fetchone()[0] == 16384
        connection.executemany(
            "INSERT INTO vectors(part_id, embedding) VALUES (?, ?)",
            [(1, far), (2, middle), (3, near), (99, near)],
        )
        connection.executemany(
            "INSERT INTO eligible(part_id) VALUES (?)", [(1,), (2,), (3,)]
        )
        connection.executemany(
            "INSERT INTO captions(part_id, text) VALUES (?, ?)",
            [(1, "garden"), (2, "car turning"), (3, "car arrives")],
        )
        rows = connection.execute(
            "WITH authorized AS (SELECT part_id FROM eligible) "
            "SELECT v.part_id, vec1_cos_distance(v.embedding, ?) AS distance "
            "FROM authorized a JOIN vectors v ON v.part_id=a.part_id "
            "ORDER BY distance, v.part_id LIMIT 20",
            (query,),
        ).fetchall()
        assert [row[0] for row in rows] == [3, 2, 1]
        assert [row[1] for row in rows] == pytest.approx([0.0, 0.2, 1.0], abs=1e-6)
        assert connection.execute(
            "SELECT part_id FROM captions WHERE captions MATCH 'car' ORDER BY rank"
        ).fetchall() == [(2,), (3,)]
        connection.commit()

    with sqlite3.connect(database) as reopened:
        native.configure_retrieval_connection(reopened)
        assert reopened.execute(
            "SELECT vec1_cos_distance(embedding, ?) FROM vectors WHERE part_id=3",
            (query,),
        ).fetchone()[0] == pytest.approx(0.0, abs=1e-6)
        assert (
            reopened.execute(
                "SELECT count(*) FROM captions WHERE captions MATCH 'car'"
            ).fetchone()[0]
            == 2
        )


def test_vector_boundary_rejects_invalid_values_and_shape() -> None:
    assert len(native.pack_vector([1.0, 0.0, 0.0], dimensions=3)) == 12
    with pytest.raises(ValueError, match="expected 3 vector dimensions"):
        native.pack_vector([1.0, 0.0], dimensions=3)
    with pytest.raises(ValueError, match="finite"):
        native.pack_vector([math.nan, 0.0, 1.0], dimensions=3)
    with pytest.raises(ValueError, match="finite"):
        native.pack_vector([math.inf, 0.0, 1.0], dimensions=3)
    with pytest.raises(ValueError, match="unit-normalized"):
        native.pack_vector([1.0, 1.0, 0.0], dimensions=3)
    with pytest.raises(ValueError, match="tolerance"):
        native.pack_vector([1.0, 0.0, 0.0], dimensions=3, norm_tolerance=-1)
    with pytest.raises(ValueError, match="expected 12 vector bytes"):
        native.validate_vector_blob(b"\x00" * 8, dimensions=3)


def test_vector_blob_round_trip_is_little_endian() -> None:
    blob = native.pack_vector([1.0, 0.0, 0.0], dimensions=3)
    assert blob == b"\x00\x00\x80?" + b"\x00" * 8
    assert native.validate_vector_blob(blob, dimensions=3) == blob


def test_generic_cache_keeps_nonunit_vectors_without_weakening_model_validation() -> (
    None
):
    blob = native.pack_vector([3.0, 4.0], dimensions=2, require_unit_norm=False)
    assert (
        native.validate_vector_blob(blob, dimensions=2, require_unit_norm=False) == blob
    )
    with pytest.raises(ValueError, match="unit-normalized"):
        native.validate_vector_blob(blob, dimensions=2)
    with pytest.raises(ValueError, match="finite"):
        native.pack_vector([math.nan, 4.0], dimensions=2, require_unit_norm=False)
