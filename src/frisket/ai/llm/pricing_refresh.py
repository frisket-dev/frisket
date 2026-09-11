"""Non-blocking daily refresh of Frisket's normalized pricing catalog."""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .pricing import install_pricing_data

PRICING_URL = (
    "https://raw.githubusercontent.com/frisket-dev/frisket/main/"
    "src/frisket/ai/llm/pricing_data.json"
)
REFRESH_SECONDS = 24 * 60 * 60
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_started = False
_start_lock = threading.Lock()


def pricing_cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "frisket" / "pricing"


def _read_catalog(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("pricing catalog must be an object")
    return data


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def refresh_pricing_once(
    *,
    cache_dir: Path | None = None,
    now: float | None = None,
    fetch: Callable[[str], bytes] | None = None,
) -> bool:
    """Refresh when the persistent 24-hour attempt window has elapsed."""
    root = cache_dir or pricing_cache_dir()
    catalog_path = root / "pricing_data.json"
    attempt_path = root / "last_attempt"
    current = time.time() if now is None else now
    try:
        if current - attempt_path.stat().st_mtime < REFRESH_SECONDS:
            return False
    except FileNotFoundError:
        pass
    except OSError:
        return False
    try:
        root.mkdir(parents=True, exist_ok=True)
        attempt_path.touch()
        os.utime(attempt_path, (current, current))
        if fetch is None:
            request = urllib.request.Request(
                PRICING_URL, headers={"User-Agent": "frisket-pricing/1"}
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        else:
            payload = fetch(PRICING_URL)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError("pricing catalog exceeds size limit")
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("pricing catalog must be an object")
        install_pricing_data(data)
        _atomic_write(catalog_path, payload)
        return True
    except Exception:
        return False


def _refresh_loop(root: Path) -> None:
    while True:
        attempt_path = root / "last_attempt"
        try:
            delay = REFRESH_SECONDS - (time.time() - attempt_path.stat().st_mtime)
        except OSError:
            delay = 0
        if delay > 0:
            time.sleep(delay)
        refresh_pricing_once(cache_dir=root)


def start_pricing_refresh() -> None:
    """Load a valid cached catalog and start one daemon refresher per process."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    root = pricing_cache_dir()
    try:
        install_pricing_data(_read_catalog(root / "pricing_data.json"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    threading.Thread(
        target=_refresh_loop,
        args=(root,),
        name="frisket-pricing-refresh",
        daemon=True,
    ).start()
