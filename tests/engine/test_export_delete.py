"""Project export and delete endpoints.

GET  /api/projects/{pid}/export        — stream the bundle as a zip
DELETE /api/projects/{pid}              — remove the bundle from disk

Deletion is OUTSIDE the op log (the log lives in the deleted db), so it is
irreversible and demands that the caller type the project name back in the
request body ({"confirm_name": ...}) — a 422 when it is missing or wrong,
mirroring how undo refuses barrier ops.
"""

import io
import json
import sqlite3
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.blob_backend import FilesystemProjectBlobStore

CSV = "name,score\nAda,5\nGrace,4\n"


class _FalseyFilesystemStore(FilesystemProjectBlobStore):
    def __bool__(self) -> bool:
        return False


class _SignalingFilesystemStore(FilesystemProjectBlobStore):
    def __init__(self, *, root: Path):
        super().__init__(root=root)
        self.signal_puts = False
        self.put_finished = threading.Event()

    def put(self, data: bytes) -> str:
        digest = super().put(data)
        if self.signal_puts:
            self.put_finished.set()
        return digest


@pytest.fixture
def client(replay_client):
    return replay_client


def make_project(client) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Bundle Test"}).json()["id"]
    r = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("rows.csv", CSV, "text/csv")},
    )
    assert r.status_code == 200, r.text
    return pid, r.json()["sheet_id"]


class TestExport:
    def test_falsey_injected_blob_store_never_falls_back(self, tmp_path):
        store = _FalseyFilesystemStore(root=tmp_path / "canonical-blobs")
        project = Project.create(
            tmp_path / "falsey-store.frisket",
            name="Falsey store",
            blob_store=store,
        )
        try:
            assert project.blob_store is store
            digest = project.add_blob(b"falsey stores are still valid")
            assert project.read_blob(digest) == b"falsey stores are still valid"
        finally:
            project.close()

    def test_export_db_manifest_and_blob_members_are_one_concurrent_snapshot(
        self,
        tmp_path,
        monkeypatch,
    ):
        bundle = tmp_path / "snapshot.frisket"
        store = _SignalingFilesystemStore(root=tmp_path / "canonical-blobs")
        project = Project.create(bundle, name="Snapshot", blob_store=store)
        digest_a = project.add_blob(b"snapshot member A", filename="a.bin")
        target = tmp_path / "snapshot.frisket.zip"
        db_member_reached = threading.Event()
        release_db_member = threading.Event()
        original_write = zipfile.ZipFile.write
        original_writestr = zipfile.ZipFile.writestr

        def gate_db_member() -> None:
            db_member_reached.set()
            assert release_db_member.wait(timeout=5), "export race probe timed out"

        def paused_write(archive, filename, arcname=None, *args, **kwargs):
            if arcname == "project.db":
                gate_db_member()
            return original_write(archive, filename, arcname, *args, **kwargs)

        def paused_writestr(archive, zinfo_or_arcname, data, *args, **kwargs):
            name = (
                zinfo_or_arcname
                if isinstance(zinfo_or_arcname, str)
                else zinfo_or_arcname.filename
            )
            if name == "project.db":
                gate_db_member()
            return original_writestr(archive, zinfo_or_arcname, data, *args, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, "write", paused_write)
        monkeypatch.setattr(zipfile.ZipFile, "writestr", paused_writestr)

        def add_concurrent_blob() -> str:
            reopened = Project(bundle, blob_store=store)
            try:
                return reopened.add_blob(b"concurrent member B", filename="b.bin")
            finally:
                reopened.close()

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                exported = pool.submit(project.export, target, True)
                assert db_member_reached.wait(timeout=5)
                store.signal_puts = True
                writer = pool.submit(add_concurrent_blob)
                assert store.put_finished.wait(timeout=5)
                digest_b = writer.result(timeout=5)
                release_db_member.set()
                assert exported.result(timeout=5) == target

            with zipfile.ZipFile(target) as archive:
                manifest_hashes = set(
                    json.loads(archive.read("manifest.json"))["blobs"]
                )
                member_hashes = {
                    Path(name).name
                    for name in archive.namelist()
                    if name.startswith("blobs/")
                }
                snapshot_db = tmp_path / "snapshot.db"
                snapshot_db.write_bytes(archive.read("project.db"))
            connection = sqlite3.connect(snapshot_db)
            try:
                db_hashes = {
                    row[0] for row in connection.execute("SELECT hash FROM blobs")
                }
            finally:
                connection.close()

            assert db_hashes == manifest_hashes == member_hashes == {digest_a}
            assert digest_b not in db_hashes
        finally:
            release_db_member.set()
            project.close()

    def test_export_streams_bundle_zip(self, client):
        pid, _ = make_project(client)
        r = client.get(f"/api/projects/{pid}/export")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/zip"
        disposition = r.headers.get("content-disposition", "")
        assert "attachment" in disposition
        assert ".frisket.zip" in disposition
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "project.db" in names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["format"] == "frisket-bundle"

    def test_export_round_trips_via_import_bundle(self, client, tmp_path):
        pid, _ = make_project(client)
        r = client.get(f"/api/projects/{pid}/export")
        assert r.status_code == 200
        zip_path = tmp_path / "out.frisket.zip"
        zip_path.write_bytes(r.content)
        p = Project.import_bundle(zip_path, tmp_path / "restored.frisket")
        try:
            sheets = p.sheets()
            assert [s["name"] for s in sheets] == ["rows"]
            assert p.row_count(sheets[0]["id"]) == 2
        finally:
            p.close()

    def test_export_include_media_param(self, client):
        pid, _ = make_project(client)
        r = client.post(
            f"/api/projects/{pid}/import/files",
            files=[("files", ("a.txt", b"hello blob", "text/plain"))],
        )
        assert r.status_code == 200, r.text
        full = zipfile.ZipFile(
            io.BytesIO(client.get(f"/api/projects/{pid}/export").content)
        )
        assert any(n.startswith("blobs/") for n in full.namelist())
        slim = zipfile.ZipFile(
            io.BytesIO(
                client.get(f"/api/projects/{pid}/export?include_media=false").content
            )
        )
        assert not any(n.startswith("blobs/") for n in slim.namelist())

    def test_export_unknown_project_404(self, client):
        assert client.get("/api/projects/nope/export").status_code == 404


class TestDbOnlyExport:
    def test_db_mode_streams_raw_sqlite_snapshot(self, client, tmp_path):
        pid, _ = make_project(client)
        blob = client.post(
            f"/api/projects/{pid}/import/files",
            files=[("files", ("a.txt", b"hello blob", "text/plain"))],
        )
        assert blob.status_code == 200, blob.text

        r = client.get(f"/api/projects/{pid}/export?mode=db")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/vnd.sqlite3"
        disposition = r.headers.get("content-disposition", "")
        assert "attachment" in disposition
        assert ".frisket.db" in disposition
        assert r.content.startswith(b"SQLite format 3\x00")
        assert b"manifest.json" not in r.content
        assert b"hello blob" not in r.content
        with pytest.raises(zipfile.BadZipFile):
            zipfile.ZipFile(io.BytesIO(r.content)).namelist()

        db_path = tmp_path / "snapshot.frisket.db"
        db_path.write_bytes(r.content)
        conn = sqlite3.connect(db_path)
        try:
            sheet_names = [
                row[0] for row in conn.execute("SELECT name FROM sheets ORDER BY id")
            ]
            row_count = conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
        finally:
            conn.close()
        assert sheet_names == ["rows", "files"]
        assert row_count == 3

        with pytest.raises(zipfile.BadZipFile):
            Project.import_bundle(db_path, tmp_path / "not-a-bundle.frisket")

    def test_db_mode_unknown_project_404(self, client):
        assert client.get("/api/projects/nope/export?mode=db").status_code == 404

    def test_invalid_export_mode_rejected(self, client):
        pid, _ = make_project(client)
        r = client.get(f"/api/projects/{pid}/export?mode=archive")
        assert r.status_code == 422

    def test_actions_export_links_database_mode(self, client):
        pid, _ = make_project(client)
        r = client.get(f"/api/projects/{pid}/actions/export")
        assert r.status_code == 200, r.text
        exports = r.json()["exports"]
        assert exports["database"].endswith("/export?mode=db")


class TestDelete:
    @staticmethod
    def _delete(client, pid, confirm_name="Bundle Test"):
        return client.request(
            "DELETE", f"/api/projects/{pid}", json={"confirm_name": confirm_name}
        )

    def test_delete_requires_name_confirmation(self, client):
        pid, _ = make_project(client)
        # Missing body: FastAPI rejects the unprovided confirmation field.
        missing = client.request("DELETE", f"/api/projects/{pid}")
        assert missing.status_code == 422
        # Wrong name: refused with the danger-zone message.
        wrong = self._delete(client, pid, confirm_name="not the name")
        assert wrong.status_code == 422
        assert "type the project name" in wrong.json()["detail"]
        # nothing was touched
        assert any(p["id"] == pid for p in client.get("/api/projects").json())

    def test_delete_removes_disk_and_listing(self, client):
        pid, _ = make_project(client)
        ws = client.app.state.workspace
        bundle = ws.root / f"{pid}.frisket"
        assert bundle.exists()
        r = self._delete(client, pid)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "deleted": pid}
        # disk + listing + cached handle all consistent: no resurrection path
        assert not bundle.exists()
        assert all(p["id"] != pid for p in client.get("/api/projects").json())
        assert client.get(f"/api/projects/{pid}/sheets").status_code == 404
        assert pid not in ws._projects  # noqa: SLF001 — asserting teardown

    def test_delete_unknown_project_404(self, client):
        assert self._delete(client, "nope", confirm_name="x").status_code == 404

    def test_delete_cancels_and_drops_live_run_handles(self, client):
        from frisket.engine.runner import RunProgress

        pid, _ = make_project(client)
        ws = client.app.state.workspace
        mine = RunProgress(run_id=1, total=5)
        ws.active_runs[(pid, 1)] = mine
        other = RunProgress(run_id=1, total=5)
        ws.active_runs[("other-project", 1)] = other
        r = self._delete(client, pid)
        assert r.status_code == 200
        assert (pid, 1) not in ws.active_runs
        assert mine.cancelled is True
        # cross-project isolation: another project's run 1 is untouched
        assert ("other-project", 1) in ws.active_runs
        assert other.cancelled is False

    def test_delete_frees_the_slug_for_recreation(self, client):
        pid, _ = make_project(client)
        self._delete(client, pid)
        out = client.post("/api/projects", json={"name": "Bundle Test"}).json()
        assert out["id"] == pid  # not bundle-test-2: the path is truly free
