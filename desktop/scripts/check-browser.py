#!/usr/bin/env python3
"""Prove explicit setup installed the private runtime's matching Chromium."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).resolve(strict=True)))

from frisket.runtime.supervisor import spawn_service, stop_service  # noqa: E402

# The supervisor owns the Playwright driver and Chromium descendants on both
# platforms, including when the prewarm runner exits before the check finishes.
program = """
import sys
from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 640, "height": 360})
        page.route("**/*", lambda route: route.abort())
        page.set_content("<html><body><h1>Frisket browser setup</h1></body></html>")
        assert page.locator("h1").inner_text() == "Frisket browser setup"
        image = page.screenshot(path=sys.argv[1])
        assert image.startswith(bytes.fromhex("89504e470d0a1a0a"))
        print("Explicit Chromium setup: matching binary launched and captured a local fixture.")
    finally:
        browser.close()
"""
child = spawn_service(
    [sys.executable, "-I", "-B", "-c", program, sys.argv[2]],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    env=dict(os.environ),
)
try:
    output, errors = child.communicate(timeout=60)
finally:
    stop_service(child)
if child.returncode:
    raise RuntimeError(f"Chromium launch proof failed: {errors.decode(errors='replace')}")
print(output.decode(), end="")
