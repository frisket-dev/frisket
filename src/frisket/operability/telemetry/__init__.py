"""Small, closed product-telemetry boundary.

Events are advisory product counters. The browser observes them; the server
validates their closed vocabulary, adds trusted release facts, and forwards
them best-effort. This module deliberately has no queue, retry, lifecycle, or
destination registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import logging
import os
import re
from typing import Any, Literal, Protocol

import httpx


LOG = logging.getLogger("frisket.telemetry")
TelemetryContext = Literal["installation", "project"]
Edition = Literal["solo", "team", "cloud"]

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
    "Import.finished": (
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
    "Export.requested": ("project", frozenset({"exportKind"})),
}


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


@dataclass(frozen=True)
class ProductTelemetryRuntime:
    destination: TelemetryDestination | None
    edition: Edition
    version: str

    @property
    def available(self) -> bool:
        return self.destination is not None

    def emit(self, monthly_id: str, event: ProductTelemetryEvent) -> bool:
        if self.destination is None:
            return False
        if MONTHLY_ID_PATTERN.fullmatch(monthly_id) is None:
            raise ValueError("invalid monthly telemetry identifier")
        signal = {
            "appID": getattr(self.destination, "app_id", "recording"),
            "clientUser": monthly_id,
            "type": event.type,
            "payload": {
                "Frisket.schemaVersion": "1",
                "Frisket.version": self.version,
                "Frisket.edition": self.edition,
                **{f"Frisket.{key}": value for key, value in event.properties.items()},
            },
        }
        try:
            self.destination.send(signal)
        except Exception:  # noqa: BLE001 - telemetry cannot affect the product
            LOG.warning("product_telemetry_delivery_failed")
            return False
        return True


def _package_version() -> str:
    from frisket import DISTRIBUTION_NAME

    try:
        value = metadata.version(DISTRIBUTION_NAME).strip()
    except metadata.PackageNotFoundError:
        return "development"
    return value or "development"


def build_product_telemetry_runtime(
    *,
    edition: Edition = "solo",
    destination: TelemetryDestination | None = None,
) -> ProductTelemetryRuntime:
    if os.environ.get("FRISKET_TELEMETRY", "").strip().lower() == "off":
        return ProductTelemetryRuntime(
            destination=None,
            edition=edition,
            version=_package_version(),
        )
    if destination is None:
        namespace = os.environ.get("FRISKET_TELEMETRYDECK_NAMESPACE", "").strip()
        app_id = os.environ.get("FRISKET_TELEMETRYDECK_APP_ID", "").strip()
        if namespace and app_id and re.fullmatch(r"[A-Za-z0-9_.-]+", namespace):
            destination = TelemetryDeckDestination(namespace=namespace, app_id=app_id)
    return ProductTelemetryRuntime(
        destination=destination,
        edition=edition,
        version=_package_version(),
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
