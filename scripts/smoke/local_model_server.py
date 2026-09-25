#!/usr/bin/env python3
"""Exercise the installed CPU Docling server with text and scanned PDFs."""

from __future__ import annotations

import argparse
import os
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
from PIL import Image, ImageDraw
from PIL import ImageFont

from frisket_models.local import create_app


def _scanned_pdf(path: Path) -> None:
    image = Image.new("RGB", (1400, 500), "white")
    font = ImageFont.load_default(size=72)
    ImageDraw.Draw(image).text(
        (80, 180),
        "FRISKET SCANNED OCR PROOF 2026",
        fill="black",
        font=font,
    )
    image.save(path, "PDF", resolution=150)


def _convert(client, token: str, path: Path) -> dict:
    with path.open("rb") as handle:
        response = client.post(
            "/to-markdown",
            headers={"Authorization": f"Bearer {token}"},
            data={"engine": "docling"},
            files={"files": (path.name, handle, "application/pdf")},
        )
    response.raise_for_status()
    [document] = response.json()["documents"]
    return document


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text-pdf", type=Path, required=True)
    parser.add_argument("--url")
    parser.add_argument("--token")
    parser.add_argument("--lifecycle-install", action="store_true")
    args = parser.parse_args()
    if bool(args.url) != bool(args.token):
        parser.error("--url and --token must be supplied together")
    if args.lifecycle_install and args.url:
        parser.error("--lifecycle-install cannot be combined with --url")

    server = None
    if args.lifecycle_install:
        from frisket.runtime.model_install import install_docling
        from frisket.runtime.model_server import LocalModelServer

        server = LocalModelServer(environ=os.environ)
        # Prove the running app's monitor notices an install that completes
        # after application startup; no browser-owned continuation is needed.
        server.start()
        install_docling(should_cancel=lambda: False, progress=print)
        args.url = server.url
        args.token = os.environ["FRISKET_LOCAL_MODELS_TOKEN"]
        deadline = time.monotonic() + 30
        while True:
            try:
                response = httpx.get(
                    f"{args.url}/capabilities",
                    headers={"Authorization": f"Bearer {args.token}"},
                    timeout=1,
                )
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "local model server did not become ready after install"
                )
            time.sleep(0.1)

    token = args.token or "local-model-smoke-token"

    try:
        with tempfile.TemporaryDirectory() as temporary:
            scanned = Path(temporary) / "scanned.pdf"
            _scanned_pdf(scanned)
            if args.url:
                client_context = httpx.Client(base_url=args.url, timeout=120)
            else:
                os.environ["FRISKET_LOCAL_MODELS_TOKEN"] = token
                client_context = TestClient(create_app())
            with client_context as client:
                text = _convert(client, token, args.text_pdf)
                scan = _convert(client, token, scanned)
    finally:
        if server is not None:
            server.stop()

    assert text["markdown"].strip(), text
    assert text["ocr_used"] and not any(text["ocr_used"]), text
    assert "FRISKET" in scan["markdown"].upper(), scan
    assert scan["ocr_used"] and any(scan["ocr_used"]), scan
    print(
        "local model smoke: OK; "
        f"text_ocr={text['ocr_used']}; scan_ocr={scan['ocr_used']}"
    )


if __name__ == "__main__":
    main()
