"""Queue initialization/use regressions for supported SQLite memory locators."""

import pytest

from frisket.engine.jobs.queue import SqlAlchemyJobQueue


@pytest.mark.parametrize(
    "locator",
    ("sqlite://", "sqlite:///:memory:", "sqlite+pysqlite:///:memory:"),
)
def test_sqlite_memory_locator_initializes_and_claims(locator: str) -> None:
    queue = SqlAlchemyJobQueue(locator)
    try:
        queue.enqueue("memory-test", {"locator": locator})
        claimed = queue.claim("memory-worker")
        assert claimed is not None
        assert claimed.kind == "memory-test"
        assert claimed.payload == {"locator": locator}
    finally:
        queue.close()
