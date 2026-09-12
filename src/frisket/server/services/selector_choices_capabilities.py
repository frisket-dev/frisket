"""Immutable actor authority supplied by the edition composition."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import Request


@dataclass(frozen=True, slots=True)
class SelectorCapabilities:
    may_author_actions: bool = False
    may_run_actions: bool = False
    configure_workspace_credentials: bool = False
    configure_project_credentials: bool = False
    configure_organization_credentials: bool = False
    manage_model_downloads: bool = False
    configure_models_gateway: bool = False


SelectorCapabilitiesFor = Callable[[Request, str], SelectorCapabilities]
SelectorModelsGatewayStatusFor = Callable[[bool], Mapping[str, Any]]
