"""Leaf helper: every submodule under ``frisket.followthemoney`` that
hard-imports the third-party FollowTheMoney SDK (``adapter``,
``import_planner``, ``mapping``, ``schema_catalog``) wraps that import in
``require_followthemoney_sdk()`` so a missing ``entities`` extra raises a
clear, actionable ``ImportError`` naming the remediation -- never a raw
``ModuleNotFoundError`` surfacing from deep inside a third-party import.

This module has ZERO dependencies (stdlib only) so it can be imported safely
from any submodule regardless of whether the SDK is present. ``__init__.py``
re-exports ``ENTITIES_EXTRA_REMEDIATION`` from here so there is exactly one
string, not one copy per file.

The initial guard covered only the handful of EXTERNAL call sites that reach
into this package
(graph/neighborhood.py, features/followthemoney/migration_harness.py, the bundled frisket.ftm
plugin) -- a direct import of e.g. ``frisket.followthemoney.adapter`` or a
missing public re-export still raised a bare ``ModuleNotFoundError``. This
closes that gap at the actual import sites themselves.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

ENTITIES_EXTRA_REMEDIATION = "pip install 'frisket[entities]'"


@contextlib.contextmanager
def require_followthemoney_sdk() -> Iterator[None]:
    """Catches broadly, not just ``ImportError``: a present-but-broken
    install (missing/broken ``normality``, ``rigour``, or the native
    ``pyicu`` extension underneath ``followthemoney``) can fail with other
    exception types (``OSError`` from a broken shared library, ``RuntimeError``
    from a native init failure, ...). Every failure mode inside the guarded
    block becomes the SAME
    clean, actionable ``ImportError``, so a caller checking for
    ``ImportError`` (this package's own ``__init__.py``, in particular) can
    treat "present but broken" and "absent" identically."""
    try:
        yield
    except Exception as exc:  # noqa: BLE001 -- see docstring: any failure here means "not usable"
        raise ImportError(
            "FollowTheMoney entity support is not installed. Install with "
            f"{ENTITIES_EXTRA_REMEDIATION}."
        ) from exc
