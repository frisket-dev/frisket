"""Trusted connection composition for the standalone Team worker."""

from __future__ import annotations

from frisket.engine.jobs.ports import WorkerPorts
from frisket.team.document_credentials import TeamOrganizationDocumentCredentialPort
from frisket.team.config import team_config_from_env
from frisket.team.control_plane import (
    TeamOrganizationKeyCredentialPort,
    TeamOrganizationSearchKeyCredentialPort,
)
from frisket.team.gateway_routes import TeamOrganizationModelsGatewayPort
from frisket.team.secret_box import TeamSecretBox


def team_worker_ports_from_env() -> WorkerPorts:
    """Use the same per-deployment decryptor as the Team web process."""

    secret_box = TeamSecretBox(team_config_from_env())
    return WorkerPorts(
        credential_port=TeamOrganizationKeyCredentialPort(secret_box.decrypt),
        action_credential_port=TeamOrganizationDocumentCredentialPort(
            secret_box.decrypt
        ),
        search_credential_port=TeamOrganizationSearchKeyCredentialPort(
            secret_box.decrypt
        ),
        models_gateway_port=TeamOrganizationModelsGatewayPort(secret_box.decrypt),
    )
