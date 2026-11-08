"""Small, closed product-telemetry boundary.

Events are advisory product counters. The browser observes them; the server
validates their closed vocabulary, adds trusted release facts, and forwards
them best-effort. This module deliberately has no queue, retry, lifecycle, or
destination registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import metadata
import json
import logging
import os
import re
import platform
from typing import Any, Literal, Protocol

import httpx


LOG = logging.getLogger("frisket.telemetry")
TelemetryContext = Literal["installation", "project"]
Edition = Literal["solo", "team", "cloud"]

# Public ingest identifiers, not credentials. Sending still requires the user's
# preference in the browser; FRISKET_TELEMETRY=off disables the receiver entirely.
DEFAULT_NAMESPACE = "com.frisket"
DEFAULT_APP_ID = "2D6F53C7-7841-4AC9-8D8D-4434612D57CA"

MONTHLY_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")

DURATION_BUCKETS = frozenset(
    {"lt_1s", "1_10s", "10_60s", "1_10m", "gte_10m", "unknown"}
)
BYTE_BUCKETS = frozenset(
    {"zero", "lt_1mb", "1_100mb", "100mb_1gb", "gte_1gb", "unknown"}
)
ROW_BUCKETS = frozenset({"zero", "1_999", "1k_99k", "100k_999k", "gte_1m", "unknown"})
COLUMN_BUCKETS = frozenset({"zero", "1_5", "6_20", "21_100", "gte_101", "unknown"})
RESULTS = frozenset(
    {
        "success",
        "partial",
        "queued",
        "needs_confirmation",
        "rejected",
        "failed",
        "cancelled",
        "stalled",
        "orphaned",
        "no_live_worker",
    }
)
FAILURES = frozenset(
    {
        "none",
        "invalid_input",
        "unsupported_format",
        "permission",
        "missing_dependency",
        "missing_credential",
        "network",
        "provider",
        "rate_limited",
        "timeout",
        "empty_output",
        "invalid_output",
        "conflict",
        "internal",
        "unknown",
    }
)

_PROPERTY_VALUES: dict[str, frozenset[str]] = {
    "setupKind": frozenset({"engine", "model"}),
    "platform": frozenset({"linux", "macos", "windows", "ios", "android", "other"}),
    "creationKind": frozenset({"blank", "sample"}),
    "importKind": frozenset({"file", "files", "paste", "url"}),
    "format": frozenset(
        {
            "csv",
            "xlsx",
            "json",
            "jsonl",
            "parquet",
            "pdf",
            "image",
            "audio",
            "video",
            "html",
            "text",
            "mixed",
            "other",
            "unknown",
        }
    ),
    "bytes": BYTE_BUCKETS,
    "rows": ROW_BUCKETS,
    "columns": COLUMN_BUCKETS,
    "requestDuration": DURATION_BUCKETS,
    "observedDuration": DURATION_BUCKETS,
    "result": RESULTS,
    "failureCategory": FAILURES,
    "sourceKind": frozenset(
        {"rss", "youtube_playlist", "youtube_channel", "api", "courtlistener", "other"}
    ),
    "attemptKind": frozenset({"initial", "retry", "resume", "backfill"}),
    "exportKind": frozenset(
        {
            "sheet_csv",
            "sheet_xlsx",
            "project_bundle",
            "project_bundle_without_media",
            "project_database",
            "work_log",
        }
    ),
}

_EVENT_FIELDS: dict[str, tuple[TelemetryContext, frozenset[str]]] = {
    "App.opened": ("installation", frozenset({"platform"})),
    "Project.created": ("project", frozenset({"creationKind", "requestDuration"})),
    "Project.createFailed": (
        "installation",
        frozenset({"creationKind", "requestDuration", "failureCategory"}),
    ),
    "Project.opened": ("project", frozenset()),
    "Import.started": ("project", frozenset({"importKind", "format", "bytes"})),
    "Import.submitted": (
        "project",
        frozenset(
            {
                "importKind",
                "format",
                "bytes",
                "rows",
                "columns",
                "requestDuration",
                "result",
                "failureCategory",
            }
        ),
    ),
    "Source.created": (
        "project",
        frozenset({"sourceKind", "requestDuration", "result", "failureCategory"}),
    ),
    "Action.opened": ("project", frozenset({"action"})),
    "Action.runSubmitted": (
        "project",
        frozenset(
            {
                "action",
                "attemptKind",
                "rows",
                "columns",
                "requestDuration",
                "result",
                "failureCategory",
            }
        ),
    ),
    "Action.runObserved": (
        "project",
        frozenset(
            {
                "action",
                "attemptKind",
                "observedDuration",
                "result",
                "failureCategory",
            }
        ),
    ),
    "Cloud.connectionStarted": ("installation", frozenset()),
    "Cloud.connectionFinished": (
        "installation",
        frozenset({"result", "failureCategory"}),
    ),
    "Setup.submitted": (
        "installation",
        frozenset({"setupKind", "result", "failureCategory"}),
    ),
    "Setup.observed": (
        "installation",
        frozenset({"setupKind", "result", "observedDuration", "failureCategory"}),
    ),
    "Export.requested": ("project", frozenset({"exportKind"})),
}


# Older released clients use this name even for queued admissions.
_EVENT_FIELDS["Import.finished"] = _EVENT_FIELDS["Import.submitted"]


def _bucket(value: int | None, edges: tuple[tuple[int, str], ...], final: str) -> str:
    if value is None:
        return "unknown"
    if type(value) is not int or value < 0:
        raise ValueError("telemetry bucket values must be non-negative integers")
    for upper, label in edges:
        if value < upper:
            return label
    return final


def duration_bucket(milliseconds: int | None) -> str:
    return _bucket(
        milliseconds,
        ((1_000, "lt_1s"), (10_000, "1_10s"), (60_000, "10_60s"), (600_000, "1_10m")),
        "gte_10m",
    )


def byte_bucket(size: int | None) -> str:
    if size == 0:
        return "zero"
    return _bucket(
        size,
        (
            (1, "zero"),
            (1024**2, "lt_1mb"),
            (100 * 1024**2, "1_100mb"),
            (1024**3, "100mb_1gb"),
        ),
        "gte_1gb",
    )


def row_bucket(count: int | None) -> str:
    return _bucket(
        count,
        ((1, "zero"), (1_000, "1_999"), (100_000, "1k_99k"), (1_000_000, "100k_999k")),
        "gte_1m",
    )


def column_bucket(count: int | None) -> str:
    return _bucket(
        count,
        ((1, "zero"), (6, "1_5"), (21, "6_20"), (101, "21_100")),
        "gte_101",
    )


@dataclass(frozen=True)
class ProductTelemetryEvent:
    type: str
    properties: dict[str, str]

    def __post_init__(self) -> None:
        try:
            _, expected = _EVENT_FIELDS[self.type]
        except KeyError as exc:
            raise ValueError("unregistered product telemetry event") from exc
        if set(self.properties) != expected:
            raise ValueError("product telemetry properties do not match event")
        for key, value in self.properties.items():
            if key == "action":
                # Keep telemetry's vocabulary closed to builtins, never
                # project-installed plugin IDs. Import at event validation,
                # not while the typed registry itself is being constructed.
                from frisket.actions.registry import ACTION_REGISTRY

                allowed = set(ACTION_REGISTRY.action_ids) | {"plugin", "other"}
            else:
                allowed = _PROPERTY_VALUES[key]
            if type(value) is not str or value not in allowed:
                raise ValueError("unregistered product telemetry value")
        if (
            self.properties.get("result") == "success"
            and self.properties.get("failureCategory") != "none"
        ):
            raise ValueError("successful telemetry event cannot have a failure")

    @property
    def context(self) -> TelemetryContext:
        return _EVENT_FIELDS[self.type][0]


class TelemetryDestination(Protocol):
    def send(self, signal: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class TelemetryDeckDestination:
    namespace: str
    app_id: str
    timeout_seconds: float = 2.0

    def send(self, signal: dict[str, Any]) -> None:
        response = httpx.post(
            f"https://nom.telemetrydeck.com/v2/namespace/{self.namespace}/",
            json=[signal],
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()


@dataclass
class ProductTelemetryRuntime:
    destination: TelemetryDestination | None
    edition: Edition
    version: str
    test_mode: bool = False
    desktop_version: str | None = None
    last_delivery: dict[str, str] = field(default_factory=dict)

    def diagnostics(self) -> dict[str, Any]:
        mode = "test events" if self.test_mode else "production events"
        outcome = self.last_delivery.get("status", "no events observed")
        summary = (
            f"Configured ({mode}); last delivery: {outcome}"
            if self.available
            else "Disabled or not configured"
        )
        return {
            "summary": summary,
            "available": self.available,
            "test_mode": self.test_mode,
            "last_delivery": dict(self.last_delivery),
            "note": "Best effort, this server process only. Browser preference controls sending.",
        }

    @property
    def available(self) -> bool:
        return self.destination is not None

    def emit(
        self,
        monthly_id: str,
        event: ProductTelemetryEvent,
        *,
        client: dict[str, Any] | None = None,
    ) -> bool:
        if self.destination is None:
            return False
        if MONTHLY_ID_PATTERN.fullmatch(monthly_id) is None:
            raise ValueError("invalid monthly telemetry identifier")
        device = client or {}
        device_platform = device.get(
            "platform", event.properties.get("platform", "other")
        )
        payload = {
            "Frisket.schemaVersion": "2",
            "Frisket.version": self.version,
            "Frisket.edition": self.edition,
            "Frisket.clientKind": "browser",
            "TelemetryDeck.AppInfo.version": self.version,
            "TelemetryDeck.Device.operatingSystem": device_platform,
            "TelemetryDeck.Device.platform": device_platform,
            **{f"Frisket.{key}": value for key, value in event.properties.items()},
        }
        if device.get("browser"):
            payload["Frisket.browser"] = device["browser"]
        if device.get("browser_major"):
            payload["Frisket.browserMajorVersion"] = device["browser_major"]
        if self.desktop_version and device.get("browser") == "electron":
            payload.update(_desktop_metadata(self.desktop_version))
        signal = {
            "appID": getattr(self.destination, "app_id", "recording"),
            "clientUser": monthly_id,
            "type": event.type,
            "isTestMode": self.test_mode,
            "payload": payload,
        }
        try:
            self.destination.send(signal)
        except Exception as exc:  # noqa: BLE001 - telemetry cannot affect the product
            category = (
                "http"
                if isinstance(exc, httpx.HTTPStatusError)
                else "network"
                if isinstance(exc, httpx.TransportError)
                else "other"
            )
            self.last_delivery = {
                "status": "failed",
                "category": category,
                "at": datetime.now(timezone.utc).isoformat(),
            }
            LOG.warning(
                "product_telemetry_delivery_failed",
                extra={"failure_category": category},
            )
            return False
        self.last_delivery = {
            "status": "accepted",
            "at": datetime.now(timezone.utc).isoformat(),
        }
        LOG.debug("product_telemetry_delivery_accepted")
        return True


def _package_version() -> str:
    from frisket import DISTRIBUTION_NAME

    try:
        value = metadata.version(DISTRIBUTION_NAME).strip()
    except metadata.PackageNotFoundError:
        return "development"
    return value or "development"


def _desktop_metadata(version: str) -> dict[str, str]:
    # Only native Desktop reports native OS facts. Cloud must never label users
    # with its own server's Linux version. No hostname or full kernel string.
    system = platform.system()
    os_name = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(
        system, "other"
    )
    raw_version = platform.mac_ver()[0] if system == "Darwin" else platform.release()
    match = re.match(r"^(\d{1,4})(?:\.(\d{1,4}))?", raw_version)
    architecture = platform.machine().lower()
    architecture = {"amd64": "x86_64", "aarch64": "arm64"}.get(
        architecture, architecture
    )
    result = {
        "Frisket.clientKind": "desktop",
        "Frisket.desktopVersion": version,
        "TelemetryDeck.Device.operatingSystem": os_name,
        "TelemetryDeck.Device.platform": os_name,
        "TelemetryDeck.Device.architecture": architecture
        if architecture in {"arm64", "x86_64", "x86", "i386", "i686"}
        else "other",
    }
    if match:
        result["TelemetryDeck.Device.systemMajorVersion"] = match[1]
        result["TelemetryDeck.Device.systemMajorMinorVersion"] = (
            f"{match[1]}.{match[2] or '0'}"
        )
    return result


def _development_install() -> bool:
    from frisket import DISTRIBUTION_NAME

    try:
        direct_url = metadata.distribution(DISTRIBUTION_NAME).read_text(
            "direct_url.json"
        )
        return bool(json.loads(direct_url or "{}").get("dir_info", {}).get("editable"))
    except (metadata.PackageNotFoundError, ValueError):
        return True


def build_product_telemetry_runtime(
    *,
    edition: Edition = "solo",
    destination: TelemetryDestination | None = None,
) -> ProductTelemetryRuntime:
    disabled = os.environ.get("FRISKET_TELEMETRY", "").strip().lower() in {
        "off",
        "false",
        "0",
        "no",
        "disabled",
    }
    if disabled:
        destination = None
    elif destination is None:
        custom = (
            "FRISKET_TELEMETRYDECK_NAMESPACE" in os.environ
            or "FRISKET_TELEMETRYDECK_APP_ID" in os.environ
        )
        namespace = os.environ.get(
            "FRISKET_TELEMETRYDECK_NAMESPACE", "" if custom else DEFAULT_NAMESPACE
        ).strip()
        app_id = os.environ.get(
            "FRISKET_TELEMETRYDECK_APP_ID", "" if custom else DEFAULT_APP_ID
        ).strip()
        if namespace and app_id and re.fullmatch(r"[A-Za-z0-9_.-]+", namespace):
            destination = TelemetryDeckDestination(namespace=namespace, app_id=app_id)
    test_setting = os.environ.get("FRISKET_TELEMETRY_TEST_MODE")
    test_mode = (
        _development_install()
        if test_setting is None
        else test_setting.strip().lower() in {"1", "true", "on"}
    )
    desktop_version = os.environ.get("FRISKET_DESKTOP_VERSION", "")
    if not re.fullmatch(r"[0-9][0-9A-Za-z.+-]{0,63}", desktop_version):
        desktop_version = None
    return ProductTelemetryRuntime(
        destination=destination,
        edition=edition,
        version=_package_version(),
        test_mode=test_mode,
        desktop_version=desktop_version,
    )


__all__ = [
    "ProductTelemetryEvent",
    "ProductTelemetryRuntime",
    "TelemetryDeckDestination",
    "TelemetryDestination",
    "build_product_telemetry_runtime",
    "byte_bucket",
    "column_bucket",
    "duration_bucket",
    "row_bucket",
]
