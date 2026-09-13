"""Trusted connection composition for the standalone Team worker."""

from __future__ import annotations

from frisket.engine.jobs.ports import WorkerPorts
from frisket.team.config import team_config_from_env
from frisket.team.control_plane import TeamOrgKeyCredentialPort
from frisket.team.gateway_routes import TeamOrgModelsGatewayPort
from frisket.team.secret_box import TeamSecretBox


def team_worker_ports_from_env() -> WorkerPorts:
    """Use the same per-deployment decryptor as the Team web process."""

    secret_box = TeamSecretBox(team_config_from_env())
    return WorkerPorts(
        credential_port=TeamOrgKeyCredentialPort(secret_box.decrypt),
        models_gateway_port=TeamOrgModelsGatewayPort(secret_box.decrypt),
    )
