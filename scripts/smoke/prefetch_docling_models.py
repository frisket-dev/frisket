"""Fetch the default Docling PDF pipeline's models into the model-server cache.

Runs inside the installed model-server environment, with the server's own
child environment, before the smoke's first conversion. The Hugging Face Hub
rate-limits anonymous API calls per IP and hosted runners can hit transient
DNS/transport or 5xx errors, so HTTP 429 waits out its Retry-After and
transient network/5xx errors retry with short exponential backoff across a
bounded attempt budget; every other failure propagates unchanged. Once the
snapshots are complete in the cache, a rate-limited revision lookup during
conversion falls back to them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import socket
import sys
import time

import httpx
from huggingface_hub.errors import HfHubHTTPError

ATTEMPTS = 3
DEFAULT_WAIT_SECONDS = 60.0
MAX_WAIT_SECONDS = 180.0
TRANSIENT_BACKOFF_SECONDS = (5.0, 15.0)
_TRANSIENT_HTTP_STATUSES = frozenset({408, 500, 502, 503, 504})


def _error_chain(error: BaseException | None) -> Iterator[BaseException]:
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


def rate_limit_wait(error: BaseException | None) -> float | None:
    """Seconds the Hub asked to wait, if a 429 is anywhere in the error chain."""
    for current in _error_chain(error):
        response = getattr(current, "response", None)
        if (
            isinstance(current, (HfHubHTTPError, httpx.HTTPStatusError))
            and getattr(response, "status_code", None) == 429
        ):
            retry_after = response.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else DEFAULT_WAIT_SECONDS
            return min(wait, MAX_WAIT_SECONDS)
    return None


def is_transient_network_error(error: BaseException | None) -> bool:
    """True when the error chain contains a transient DNS/transport or 5xx error."""
    for current in _error_chain(error):
        if isinstance(
            current,
            (httpx.TransportError, ConnectionError, TimeoutError, socket.gaierror),
        ):
            return True
        if isinstance(current, (HfHubHTTPError, httpx.HTTPStatusError)):
            response = getattr(current, "response", None)
            if getattr(response, "status_code", None) in _TRANSIENT_HTTP_STATUSES:
                return True
    return False


def retry_wait(error: BaseException | None, attempt: int) -> tuple[float, str] | None:
    """Return ``(wait_seconds, reason)`` if ``error`` is retryable, else ``None``."""
    wait = rate_limit_wait(error)
    if wait is not None:
        return wait, "Hugging Face Hub rate limit"
    if is_transient_network_error(error):
        index = min(max(attempt - 1, 0), len(TRANSIENT_BACKOFF_SECONDS) - 1)
        return (
            TRANSIENT_BACKOFF_SECONDS[index],
            "transient Hugging Face Hub network error",
        )
    return None


def prefetch_with_retry(
    fetch: Callable[[], None],
    *,
    attempts: int = ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Run ``fetch`` with bounded retries on rate-limit or transient network errors."""
    for attempt in range(1, attempts + 1):
        try:
            fetch()
            return
        except Exception as error:
            decision = retry_wait(error, attempt)
            if decision is None or attempt == attempts:
                raise
            wait, reason = decision
            print(
                f"{reason} (attempt {attempt}/{attempts}): {error}; "
                f"retrying in {wait:.0f}s",
                file=sys.stderr,
            )
            sleep(wait)


def _initialize_docling_pipeline() -> None:
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter

    DocumentConverter().initialize_pipeline(InputFormat.PDF)


def main() -> None:
    prefetch_with_retry(_initialize_docling_pipeline)


if __name__ == "__main__":
    main()
