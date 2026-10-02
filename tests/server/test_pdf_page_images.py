import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.engine.store import Project
from frisket.engine.pdf_render import (
    PdfRenderCancelled,
    PdfRenderError,
    PdfRenderResult,
)
from frisket.server.services.project_blobs import ProjectBlobService
from frisket.server.routes.project_blobs import (
    register_project_blob_routes,
    _PageImageResponse,
)
from frisket.server.route_errors import register_route_error_handler


@pytest.fixture
def source(tmp_path):
    project = Project.create(tmp_path / "p.frisket", name="PDF pages")
    digest = project.add_blob(b"%PDF-fixture", filename="a.pdf", mime="application/pdf")
    service = ProjectBlobService(SimpleNamespace(get=lambda _id: project))
    try:
        yield project, digest, service
    finally:
        project.close()


def test_route_renders_only_requested_page_and_cleans_response(source, monkeypatch):
    project, digest, service = source
    paths = []

    async def render(path, scratch, **options):
        assert options["pages"] == [2]
        assert options["max_edge"] == 2000
        target = scratch / "page-2.png"
        target.write_bytes(b"image")
        paths.append(target)
        return PdfRenderResult(5, ((2, target),))

    monkeypatch.setattr(
        "frisket.server.services.project_blobs.render_pdf_pages", render
    )
    app = FastAPI()
    register_project_blob_routes(app, service=service)
    register_route_error_handler(app)
    with TestClient(app) as client:
        result = client.get(f"/api/projects/p/blobs/{digest}/pages/2/image")
        assert result.status_code == 200
        assert result.headers["content-type"] == "image/png"
        assert result.content == b"image"
        assert (
            client.get(f"/api/projects/p/blobs/{digest}/pages/0/image").status_code
            == 422
        )
        assert (
            client.get(f"/api/projects/p/blobs/{'0' * 64}/pages/1/image").status_code
            == 404
        )
    assert paths and not paths[0].parent.exists()
    assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [PdfRenderError, asyncio.CancelledError])
async def test_failed_or_cancelled_render_releases_scratch(
    source, monkeypatch, failure
):
    _project, digest, service = source
    directories = []

    async def render(path, scratch, **options):
        directories.append(scratch)
        raise failure("private filesystem detail")

    monkeypatch.setattr(
        "frisket.server.services.project_blobs.render_pdf_pages", render
    )
    with pytest.raises((asyncio.CancelledError, ValueError)) as caught:
        await service.pdf_page_image("p", digest, 1, should_cancel=lambda: False)
    if failure is PdfRenderError:
        assert caught.value.status_code == 503
        assert "private" not in caught.value.detail
    assert directories and not directories[0].exists()


@pytest.mark.asyncio
async def test_disconnect_cancels_render_and_cleans_scratch(source, monkeypatch):
    _project, digest, service = source
    directories = []

    async def render(path, scratch, **options):
        directories.append(scratch)
        for _ in range(100):
            if options["should_cancel"]():
                raise PdfRenderCancelled("cancelled")
            await asyncio.sleep(0)
        raise AssertionError("disconnect was not relayed")

    monkeypatch.setattr(
        "frisket.server.services.project_blobs.render_pdf_pages", render
    )
    app = FastAPI()
    register_project_blob_routes(app, service=service)
    route = next(route for route in app.routes if route.name == "get_pdf_page_image")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(asyncio.CancelledError):
        await route.endpoint("p", digest, SimpleNamespace(receive=receive), 1)
    assert directories and not directories[0].exists()


@pytest.mark.asyncio
async def test_response_send_failure_still_closes_owned_page(monkeypatch):
    from fastapi.responses import FileResponse

    closed = []

    async def fail(*args):
        raise OSError("connection closed")

    monkeypatch.setattr(FileResponse, "__call__", fail)
    response = _PageImageResponse(
        SimpleNamespace(path=Path("page.png"), close=lambda: closed.append(True))
    )
    with pytest.raises(OSError):
        await response({}, None, None)
    assert closed == [True]


def test_page_render_uses_same_read_authority_as_source_blob():
    from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG

    policies = {entry.route_name: entry for entry in BASE_ENDPOINT_CATALOG}
    source = policies["get_blob"]
    page = policies["get_pdf_page_image"]
    assert page.auth == source.auth
    assert page.project_role == source.project_role
    assert page.resolvers == source.resolvers


@pytest.mark.asyncio
async def test_non_pdf_refuses_before_render(source, monkeypatch):
    project, _digest, service = source
    digest = project.add_blob(b"not pdf", filename="text.txt", mime="text/plain")

    async def forbidden(*args, **kwargs):
        raise AssertionError("non-PDF must not launch a renderer")

    monkeypatch.setattr(
        "frisket.server.services.project_blobs.render_pdf_pages", forbidden
    )
    with pytest.raises(ValueError) as error:
        await service.pdf_page_image("p", digest, 1, should_cancel=lambda: False)
    assert error.value.status_code == 422
