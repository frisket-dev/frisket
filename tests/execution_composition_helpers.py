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
from frisket.execution.purchases import PurchaseAdmission


class RecordingPurchasePolicy:
    """Explicit test accounting owner for simulated platform purchases."""

    def __init__(self):
        self.admissions = []
        self.observations = []
        self.failures = []

    def prepare(self, request, credential_source):
        return request

    def admit_purchase(self, request):
        self.admissions.append(request)
        return PurchaseAdmission("metered", f"test-purchase-{len(self.admissions)}")

    def record_purchase(self, request, admission, observation):
        self.observations.append((admission, observation))

    def fail_purchase(self, request, admission, error):
        self.failures.append((admission, error))


def open_attempt_authority(project: Any, router: Any = None) -> AttemptAuthority:
    """Build routed authority with an explicit full open composition."""

    return AttemptAuthority(
        project,
        composition=open_execution_composition(
            project, router, ExecutionCompositionContext.direct()
        ),
    )
