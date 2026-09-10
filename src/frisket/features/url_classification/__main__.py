"""Snapshot regen entrypoint: ``uv run python -m frisket.features.url_classification``.

Importing the package registers the first-party matcher table; this writes the
deterministic snapshot artifact the parity check pins.
"""

from __future__ import annotations

from frisket.features.url_classification.contract import write_snapshot

if __name__ == "__main__":
    path = write_snapshot()
    print(f"wrote {path}")
