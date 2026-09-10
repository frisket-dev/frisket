"""Admitted URL acquisition for typed table producers."""

from __future__ import annotations

from typing import Protocol, Sequence

from frisket.actions.types import DynamicTableResult


class UrlImporter(Protocol):
    def read(self, urls: Sequence[str]) -> DynamicTableResult:
        """Acquire URLs into a discovered table of host-staged media values."""
        ...
