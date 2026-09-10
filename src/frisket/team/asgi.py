"""Open single-organization team ASGI entrypoint."""

from frisket.team.app import create_team_app_from_env

app = create_team_app_from_env()
