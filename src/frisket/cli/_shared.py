"""Helpers shared across the frisket CLI subcommand modules."""

from pathlib import Path


def _standalone_database_url(data_dir: Path) -> str:
    return f"sqlite:///{data_dir / 'server.sqlite3'}"
