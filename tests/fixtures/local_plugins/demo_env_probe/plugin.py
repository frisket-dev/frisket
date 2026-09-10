"""An installed Action that consumes a declared project-scoped secret.

The secret arrives through the injected `PluginSecrets` reader, never through
the process environment. The handler deliberately writes a value DERIVED from
the secret rather than the secret itself, so the tests can prove the real
value never reaches a cell, a receipt, or the configuration response.
"""

from __future__ import annotations

from pydantic import BaseModel

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult
from frisket.plugins.sdk import Plugin
from frisket.sdk import PluginSecrets

SECRET_NAME = "DEMO_API_KEY"


class ProveEnvParams(ActionParams):
    name: ColumnRef[str]


class ProveEnvOutput(BaseModel):
    env_probe: str


def prove_env(
    params: ProveEnvParams, row: Row, secrets: PluginSecrets
) -> RowResult[ProveEnvOutput]:
    del row
    # `require` raises when the secret is not configured for this project, so
    # reaching the next line is itself the proof that it was.
    secrets.require(SECRET_NAME)
    return RowResult(output=ProveEnvOutput(env_probe="configured"))


PROVE_ENV = action(
    name="prove_env",
    title="Prove project secret injection",
    description="Reads a declared project-scoped secret through the host reader.",
    category=ActionCategory.TEXT,
    run=map_rows(prove_env),
)

plugin = Plugin(
    id="demo.env_probe",
    version="0.1.0",
    capabilities=["plugin:trusted_local_backend"],
    secrets=[SECRET_NAME],
    actions=(PROVE_ENV,),
)
