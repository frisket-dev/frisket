"""Explicit open-edition composition helpers for tests.

Tests that exercise routed authority must make the deployment composition a
visible input.  Keeping this helper in tests prevents the former implicit-open
fallback from becoming a production API again.
"""

from typing import Any

from frisket.execution.attempt_authority import AttemptAuthority
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)


def open_attempt_authority(project: Any, router: Any = None) -> AttemptAuthority:
    """Build routed authority with an explicit full open composition."""

    return AttemptAuthority(
        project,
        composition=open_execution_composition(
            project, router, ExecutionCompositionContext.direct()
        ),
    )
