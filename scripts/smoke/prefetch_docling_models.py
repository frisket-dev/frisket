"""Fetch the default Docling PDF pipeline's models into the model-server cache.

Runs inside the installed model-server environment, with the server's own
child environment, before the smoke's first conversion. The Hugging Face Hub
rate-limits anonymous API calls per IP and hosted runners share IPs, so an
HTTP 429 here waits out its Retry-After and tries again, a bounded number of
times; every other failure propagates unchanged. Once the snapshots are
complete in the cache, a rate-limited revision lookup during conversion falls
back to them.
"""

from __future__ import annotations

import sys
import time

from docling.datamodel.base_models import InputFormat
from docling.document_converter import DocumentConverter
from huggingface_hub.errors import HfHubHTTPError

ATTEMPTS = 3
DEFAULT_WAIT_SECONDS = 60.0
MAX_WAIT_SECONDS = 180.0


def rate_limit_wait(error: BaseException | None) -> float | None:
    """Seconds the Hub asked to wait, if a 429 is anywhere in the error chain."""
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        response = getattr(error, "response", None)
        if (
            isinstance(error, HfHubHTTPError)
            and getattr(response, "status_code", None) == 429
        ):
            retry_after = response.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else DEFAULT_WAIT_SECONDS
            return min(wait, MAX_WAIT_SECONDS)
        error = error.__cause__ or error.__context__
    return None


def main() -> None:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            DocumentConverter().initialize_pipeline(InputFormat.PDF)
            return
        except Exception as error:
            wait = rate_limit_wait(error)
            if wait is None or attempt == ATTEMPTS:
                raise
            print(
                f"Hugging Face Hub rate limit (attempt {attempt}); retrying in {wait:.0f}s",
                file=sys.stderr,
            )
            time.sleep(wait)


if __name__ == "__main__":
    main()
