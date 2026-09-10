"""Canonical op-error registry.

The implementation lives in the CONTRACTS layer
(`frisket.contracts.actions.catalog_helpers.op_errors`) so that catalog definitions —
which call it at import time — get an `ActionErrorSpec` from the same module instance
as the catalog they populate (a contracts reload reloads both together). This module
re-exports it as the SDK-facing name used by the `@op` declarations.
"""

from __future__ import annotations

from frisket.contracts.actions.catalog_helpers import op_errors

__all__ = ["op_errors"]
