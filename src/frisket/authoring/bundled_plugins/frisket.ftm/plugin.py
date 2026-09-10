"""Dormant bundled FollowTheMoney plugin, authored with ordinary typed actions."""

from .ftm_actions import FTM_EXPORT, FTM_IMPORT

from frisket.plugins.sdk import Plugin

plugin = Plugin(
    id="frisket.ftm",
    version="1.0.0",
    capabilities=[
        "plugin:trusted_local_backend",
        "plugin:project_writes",
        "plugin:project_reads",
    ],
    auto_enable=False,
    actions=(FTM_IMPORT, FTM_EXPORT),
)
