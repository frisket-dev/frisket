"""Canonical contracts for the shared administration endpoints.

The Team CLI, Team server, and hosted control plane all consume these same
versioned envelopes. The browser administration API is the canonical wire;
the unversioned ``/api/admin/*`` paths are alternate routes, not alternate
schemas.
"""

from frisket.contracts.http.admin_browser import (
    AdminBrowserAuditV1,
    AdminBrowserErrorsV1,
    AdminBrowserJobsV1,
    AdminBrowserUsersV1,
)


AdminUsersResponse = AdminBrowserUsersV1
AdminJobsResponse = AdminBrowserJobsV1
AdminAuditResponse = AdminBrowserAuditV1
AdminErrorsResponse = AdminBrowserErrorsV1


__all__ = [
    "AdminAuditResponse",
    "AdminErrorsResponse",
    "AdminJobsResponse",
    "AdminUsersResponse",
]
