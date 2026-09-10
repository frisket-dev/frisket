"""Tiny author-facing SDK for trusted-local plugins.

Actions are declared the way built-in Actions are — ordinary `action(...)`
definitions passed to `Plugin(actions=...)`, which namespaces them under the
plugin id and hands the host the same `RegisteredAction` values the built-in
catalog holds. An installed plugin Action runs on the SAME native in-process
hosts and capabilities as a builtin; only package origin, loading, and
enablement differ.

The module also exposes the non-action workbench contributions
(`@plugin.importer`, `@plugin.projection`, `@plugin.operator`), which keep
their own subprocess handler contexts. The host owns project reads,
validation, writes, and receipts.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from frisket.actions.core import Action, RegisteredAction

from frisket.contracts.plugin import PLUGIN_ID_RE, RESERVED_PLUGIN_IDS


def register_actions(
    plugin_id: str | None, actions: Iterable[Action[Any, Any]]
) -> tuple[RegisteredAction, ...]:
    """Namespace a plugin's Action definitions under its validated plugin id.

    This is the whole of plugin action registration: an installed Action is
    an ordinary Action, admitted to the same native hosts as a builtin, so
    there is nothing to restrict here beyond identity. The plugin id must be
    a real, non-reserved namespace (a plugin cannot register into the
    built-in roster or another plugin's), and two Actions in one package
    cannot claim the same name.
    """
    definitions = tuple(actions)
    if not definitions:
        return ()
    if (
        not plugin_id
        or not PLUGIN_ID_RE.fullmatch(plugin_id)
        or plugin_id in RESERVED_PLUGIN_IDS
    ):
        raise ValueError("plugin actions require a validated namespaced plugin ID")
    if len({definition.name for definition in definitions}) != len(definitions):
        raise ValueError("duplicate plugin action name")
    # Imported lazily so a plugin that contributes only importers,
    # projections, or operators never pays for the action catalog's import
    # graph inside its subprocess.
    from frisket.actions.core import RegisteredAction

    return tuple(
        RegisteredAction(f"{plugin_id}.{definition.name}", definition)
        for definition in definitions
    )


class PluginDependencyMissing(RuntimeError):
    """Raise from a handler when it needs an optional Python dependency
    (an extra, e.g. `frisket-data[entities]`) that is not installed.

    Every OTHER exception a handler raises is redacted by the subprocess
    boundary to a generic message (`frisket.plugins.subprocess_runner`'s
    `_error_from_exception`) — a security boundary: a buggy or malicious
    plugin must not leak arbitrary details (secrets, paths, internals)
    through it. This ONE exception type is the allowlisted exception to
    that redaction: its `str(exc)` is surfaced verbatim, because by
    construction it can only ever say "install X" (whatever the handler
    passes in), never anything else. Do not raise this for any other
    purpose."""


class PluginUserError(Exception):
    """Raise from a handler when the failure is the user's to fix and the
    message alone is what they need to see — a bad input value, missing
    configuration, an explicit remediation hint.

    Like `PluginDependencyMissing`, this is an allowlisted exception to the
    subprocess redaction boundary
    (`frisket.plugins.subprocess_runner._error_from_exception`): `str(exc)`
    is surfaced VERBATIM as the handler's error, while every other
    exception type stays redacted to a generic message because arbitrary
    plugin exceptions can carry secrets, paths, or internals. This typed
    family is the explicit safe channel — put nothing in the message you
    would not show in the UI."""


class PluginRateLimited(Exception):
    """Raise when the external service rate limited the request (HTTP 429
    or a provider-specific equivalent).

    The host maps this to its retryable `plugin_provider_rate_limited`
    error and records `retry_after_seconds` (when given) as backoff before
    re-attempting. Pass either numeric seconds or the provider's
    `Retry-After` header value. The exception message is NOT surfaced across
    the redaction boundary — only the retry metadata is."""

    def __init__(
        self,
        message: str = "External service rate limited the plugin request",
        *,
        retry_after_seconds: float | str | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class PluginImporter:
    kind: str
    handler_key: str
    title: str
    description: str
    columns: tuple[dict[str, Any], ...]
    func: Callable[..., Any]


@dataclass(frozen=True)
class PluginProjection:
    kind: str
    handler_key: str
    title: str
    description: str
    func: Callable[..., Any]


@dataclass(frozen=True)
class PluginOperator:
    kind: str
    handler_key: str
    title: str
    description: str
    func: Callable[..., Any]


def _provisioned_secret(declared: tuple[str, ...], name: str) -> str:
    """`ctx.secret` backing: distinguishes an authoring bug (the name is not
    in the manifest's `requires.secrets`, so the host never injects it) from
    a provisioning gap (declared, but no value configured for this project).
    The host injects declared+provisioned secrets as env vars on the
    subprocess (workbench/plugin_subprocess.py `_project_plugin_env`)."""
    if name not in declared:
        declared_note = ", ".join(repr(item) for item in declared) or "none"
        raise PluginUserError(
            f"Plugin secret {name!r} is not declared in the manifest's "
            f"requires.secrets (declared: {declared_note}); declare it with "
            "Plugin(secrets=[...]) and rebuild plugin.json"
        )
    value = os.environ.get(name, "").strip()
    if not value:
        raise PluginUserError(
            f"Plugin secret {name!r} is declared in requires.secrets but no "
            "value is provisioned; configure it in the plugin's settings "
            "before running"
        )
    return value


@dataclass(frozen=True)
class _PluginHandlerContext:
    """Shared fields of the three per-kind subprocess handler contexts below.

    Each subclass adds exactly one kind-named field (`projection_kind`,
    `operator_kind`, `importer_kind`) — kept distinct
    rather than unified into one shared name because handler code reads it
    by that name (e.g. `frisket.geo`'s bundled plugin reads
    `ctx.projection_kind`); renaming would be a calling-convention change,
    not a mechanical one.
    """

    project_id: str
    plugin_id: str
    handler_key: str
    capabilities: tuple[str, ...]
    # kw-only so subclasses may add required positional fields after this
    # defaulted one; every construction site passes keywords already.
    secrets: tuple[str, ...] = field(default=(), kw_only=True)

    def secret(self, name: str) -> str:
        """Value of a manifest-declared secret the host provisioned for this
        run (`requires.secrets` -> injected env var). Raises PluginUserError
        naming the manifest requirement when the secret is undeclared, or the
        provisioning gap when declared but unset."""
        return _provisioned_secret(self.secrets, name)


class PluginImporterError(Exception):
    def __init__(self, diagnostic: dict[str, Any]) -> None:
        super().__init__(str(diagnostic.get("message") or "Plugin importer failed"))
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class PluginProjectionContext(_PluginHandlerContext):
    projection_kind: str


@dataclass(frozen=True)
class PluginOperatorContext(_PluginHandlerContext):
    operator_kind: str


@dataclass(frozen=True)
class PluginImporterContext(_PluginHandlerContext):
    importer_kind: str
    diagnostics: list[dict[str, Any]]

    def source_line(
        self,
        line: int,
        *,
        field: str | None = None,
        object_id: str | None = None,
    ) -> dict[str, Any]:
        attach: dict[str, Any] = {"kind": "source_line", "line": int(line)}
        if field:
            attach["field"] = field
        if object_id:
            attach["object"] = object_id
        return attach

    def warning(
        self,
        code: str,
        message: str,
        *,
        attach: dict[str, Any] | None = None,
    ) -> None:
        self.diagnostics.append(_diagnostic("warning", code, message, attach=attach))

    def error(
        self,
        code: str,
        message: str,
        *,
        attach: dict[str, Any] | None = None,
    ) -> PluginImporterError:
        return PluginImporterError(_diagnostic("error", code, message, attach=attach))


class PluginImportSource:
    def __init__(self, source: dict[str, Any], handler_params: dict[str, Any]) -> None:
        self.source = dict(source)
        self.handler_params = dict(handler_params)

    @property
    def path(self) -> Path:
        raw = self.handler_params.get("path") or self.source.get("path")
        if not isinstance(raw, str) or not raw:
            raise ValueError("plugin import source path is required")
        return Path(raw)

    async def text_lines(
        self,
        *,
        numbered: bool = False,
        encoding: str = "utf-8",
    ):
        def open_file():
            return self.path.open("r", encoding=encoding)

        with await asyncio.to_thread(open_file) as handle:
            line_no = 0
            while True:
                line = await asyncio.to_thread(handle.readline)
                if line == "":
                    return
                line_no += 1
                text = line[:-1] if line.endswith("\n") else line
                if numbered:
                    yield line_no, text
                else:
                    yield text


class Plugin:
    def __init__(
        self,
        *,
        id: str | None = None,  # noqa: A002 - mirrors the manifest field name
        version: str | None = None,
        capabilities: list[str] | tuple[str, ...] = (),
        secrets: list[str] | tuple[str, ...] = (),
        settings: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
        auto_enable: bool = True,
        actions: Iterable[Action[Any, Any]] = (),
    ) -> None:
        # Declaring id+version makes this Plugin the single source of truth
        # for a backend-only plugin.json: `frisket plugin build` generates
        # the manifest from these fields plus the registrations below
        # (frisket.plugins.manifest_generate), and the drift check switches
        # to byte-equality against that regeneration. A Plugin without them
        # (config.mjs-built plugins, test fixtures) keeps the manifest as a
        # separately authored artifact.
        self.id = id
        self.version = version
        self.capabilities = tuple(capabilities)
        self.secrets = tuple(secrets)
        self.settings = tuple(dict(item) for item in settings)
        self.auto_enable = auto_enable
        self._importers: dict[tuple[str, str], PluginImporter] = {}
        self._projections: dict[tuple[str, str], PluginProjection] = {}
        self._operators: dict[tuple[str, str], PluginOperator] = {}
        self.actions: tuple[RegisteredAction, ...] = register_actions(id, actions)

    def action_for(self, action_id: str) -> RegisteredAction | None:
        """The registered Action with this namespaced id, or None."""
        for registered in self.actions:
            if registered.action_id == action_id:
                return registered
        return None

    @property
    def declares_manifest(self) -> bool:
        """True when this Plugin carries the manifest-level fields, making it
        the generation source for plugin.json."""
        return bool(self.id and self.version)

    @property
    def importers(self) -> tuple[PluginImporter, ...]:
        return tuple(self._importers.values())

    @property
    def projections(self) -> tuple[PluginProjection, ...]:
        return tuple(self._projections.values())

    @property
    def operators(self) -> tuple[PluginOperator, ...]:
        return tuple(self._operators.values())

    def importer(
        self,
        name: str | None = None,
        *,
        kind: str | None = None,
        handler_key: str | None = None,
        title: str | None = None,
        description: str = "",
        columns: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        importer_kind = str(kind or name or "")
        importer_handler_key = str(handler_key or name or importer_kind)
        if not importer_kind:
            raise ValueError("plugin.importer requires a local importer name or kind")
        importer_title = str(title or importer_kind.replace("_", " ").title())
        importer_columns = tuple(dict(item) for item in columns)

        def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
            key = (importer_kind, importer_handler_key)
            if key in self._importers:
                raise ValueError(
                    f"duplicate plugin importer: {importer_kind} {importer_handler_key}"
                )
            self._importers[key] = PluginImporter(
                kind=importer_kind,
                handler_key=importer_handler_key,
                title=importer_title,
                description=description,
                columns=importer_columns,
                func=func,
            )
            return func

        return decorate

    def projection(
        self,
        name: str | None = None,
        *,
        kind: str | None = None,
        handler_key: str | None = None,
        title: str | None = None,
        description: str = "",
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        projection_kind = str(kind or name or "")
        projection_handler_key = str(handler_key or name or projection_kind)
        if not projection_kind:
            raise ValueError(
                "plugin.projection requires a local projection name or kind"
            )
        projection_title = str(title or projection_kind.replace("_", " ").title())

        def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
            key = (projection_kind, projection_handler_key)
            if key in self._projections:
                raise ValueError(
                    f"duplicate plugin projection: {projection_kind} "
                    f"{projection_handler_key}"
                )
            self._projections[key] = PluginProjection(
                kind=projection_kind,
                handler_key=projection_handler_key,
                title=projection_title,
                description=description,
                func=func,
            )
            return func

        return decorate

    def operator(
        self,
        name: str | None = None,
        *,
        kind: str | None = None,
        handler_key: str | None = None,
        title: str | None = None,
        description: str = "",
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        operator_kind = str(kind or name or "")
        operator_handler_key = str(handler_key or name or operator_kind)
        if not operator_kind:
            raise ValueError("plugin.operator requires a local operator name or kind")
        operator_title = str(title or operator_kind.replace("_", " ").title())

        def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
            key = (operator_kind, operator_handler_key)
            if key in self._operators:
                raise ValueError(
                    f"duplicate plugin operator: {operator_kind} {operator_handler_key}"
                )
            self._operators[key] = PluginOperator(
                kind=operator_kind,
                handler_key=operator_handler_key,
                title=operator_title,
                description=description,
                func=func,
            )
            return func

        return decorate

    def importer_for(
        self,
        *,
        kind: str,
        handler_key: str,
        plugin_id: str | None = None,
    ) -> PluginImporter | None:
        for candidate in _candidate_handler_keys(
            kind=kind,
            handler_key=handler_key,
            plugin_id=plugin_id,
        ):
            importer = self._importers.get(candidate)
            if importer is not None:
                return importer
        return None

    def projection_for(
        self,
        *,
        kind: str,
        handler_key: str,
        plugin_id: str | None = None,
    ) -> PluginProjection | None:
        for candidate in _candidate_handler_keys(
            kind=kind,
            handler_key=handler_key,
            plugin_id=plugin_id,
        ):
            projection = self._projections.get(candidate)
            if projection is not None:
                return projection
        return None

    def operator_for(
        self,
        *,
        kind: str,
        handler_key: str,
        plugin_id: str | None = None,
    ) -> PluginOperator | None:
        for candidate in _candidate_handler_keys(
            kind=kind,
            handler_key=handler_key,
            plugin_id=plugin_id,
        ):
            operator = self._operators.get(candidate)
            if operator is not None:
                return operator
        return None


def _diagnostic(
    level: str,
    code: str,
    message: str,
    *,
    attach: dict[str, Any] | None,
) -> dict[str, Any]:
    diagnostic: dict[str, Any] = {
        "level": level,
        "code": str(code),
        "message": str(message),
    }
    if attach is not None:
        diagnostic["attach"] = dict(attach)
    return diagnostic


def _candidate_handler_keys(
    *,
    kind: str,
    handler_key: str,
    plugin_id: str | None,
) -> tuple[tuple[str, str], ...]:
    kinds = _dedupe((kind, _local_kind(kind, plugin_id=plugin_id)))
    handler_keys = _dedupe((handler_key, _local_handler_key(handler_key, plugin_id)))
    return tuple(
        (candidate_kind, candidate_handler)
        for candidate_kind in kinds
        for candidate_handler in handler_keys
    )


def _local_kind(kind: str, *, plugin_id: str | None) -> str:
    """Strip a manifest kind (`<plugin id>.<family>.<local>`) back to the
    local name its decorator registered under."""
    if plugin_id:
        prefix = f"{plugin_id}."
        if kind.startswith(prefix):
            local = kind[len(prefix) :]
            for local_prefix in ("importer.", "operator.", "projection."):
                if local.startswith(local_prefix):
                    return local[len(local_prefix) :]
            return local
    for marker in (".importer.", ".operator.", ".projection."):
        if marker in kind:
            return kind.rsplit(marker, 1)[1]
    return kind


def _local_handler_key(handler_key: str, plugin_id: str | None) -> str:
    if plugin_id:
        prefix = f"{plugin_id}:"
        if handler_key.startswith(prefix):
            return handler_key[len(prefix) :]
    if ":" in handler_key:
        return handler_key.split(":", 1)[1]
    return handler_key


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return tuple(out)
