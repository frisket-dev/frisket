"""Project lifecycle services for local server routes."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from filelock import FileLock
from frisket.contracts.http.models import ProjectList
from pydantic import ValidationError

from frisket.ops.egress_policy import (
    MEDIA_PRIVATE_HOSTS_ENV,
    media_egress_policy,
    private_hosts_locked,
)
from frisket.project_identity import validate_project_slug
from frisket.project_settings import patch_project_settings
from frisket.server.route_errors import RouteError
from frisket.team.security.secrets import encrypt_secret, key_hint
from frisket.server import provider_config
from frisket.engine.store.project import SUPPORTED_EVIDENCE_RETENTION_VALUES
from frisket.engine.store.sheet_lifecycle import (
    SheetDeleteBlocked,
    SheetDeleteNotFound,
)
from frisket.features.graph.sheet_graph import sheet_materialized_kind
from frisket.engine.store.evidence import sheet_cited_column_ids
from frisket.engine.store.lineage import build_lineage_dag
from frisket.engine.store.text_annotations import annotated_text_column_ids
from frisket.engine.store.staleness import compute_sync_states
from frisket.server.workspace import Workspace
from frisket.server.services.sheet_grid import require_visible_sheet
from frisket.authoring.workbench.plugin_runtime_status import (
    workbench_plugin_runtime_index,
)

LOG = logging.getLogger("frisket.server")


class ProjectNotFound(LookupError):
    pass


class ProjectRetentionError(ValueError):
    pass


class ProjectNetworkPolicyError(ValueError):
    pass


class ProjectSettingsError(ValueError):
    pass


class ProjectDeletionRequiresConfirmation(RuntimeError):
    pass


class ProjectDeletionBlocked(RuntimeError):
    """Deletion refused because the project still has runs in flight."""

    pass


class SheetDeletionBlocked(RuntimeError):
    pass


RETENTION_POLICY_SCHEMA_VERSION = "frisket.project_retention_policy.v1"


def _retention_policy_response(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": RETENTION_POLICY_SCHEMA_VERSION,
        **policy,
        "supported_default_evidence": list(SUPPORTED_EVIDENCE_RETENTION_VALUES),
    }


NETWORK_POLICY_SCHEMA_VERSION = "frisket.project_network_policy.v1"


def _network_policy_response(project: Any) -> dict[str, Any]:
    policy = project.network_policy()
    return {
        "schemaVersion": NETWORK_POLICY_SCHEMA_VERSION,
        **policy,
        # The resolved two-state value the gate actually reads, so the UI can
        # honestly say what "inherit" currently means for this project.
        "effective": project.effective_network_policy(),
    }


def _project_settings_response(project: Any) -> dict[str, Any]:
    # The response carries the EFFECTIVE value — the stored project setting
    # resolved against the server default and lock — so the UI renders what
    # media fetches will actually do, and the locked flag so it can hide the
    # override the server refuses.
    return {
        "media_allow_private_hosts": media_egress_policy(project).allow_private_hosts,
        "media_allow_private_hosts_locked": private_hosts_locked(),
    }


PROVIDER_CATALOG: list[dict[str, Any]] = [
    {
        "id": "anthropic",
        "label": "Anthropic",
        "secret_name": "ANTHROPIC_API_KEY",
        "kind": "llm",
        "policy_fields": ["spend_cap_usd"],
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "secret_name": "OPENAI_API_KEY",
        "kind": "llm",
        "policy_fields": ["spend_cap_usd"],
    },
    {
        "id": "gemini",
        "label": "Gemini",
        "secret_name": "GEMINI_API_KEY",
        "kind": "llm",
        "policy_fields": ["spend_cap_usd"],
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "secret_name": "OPENROUTER_API_KEY",
        "kind": "llm",
        "policy_fields": ["spend_cap_usd"],
    },
]

SECRET_CONSUMER_KINDS = {"plugin", "source", "job", "mcp_connector"}


def _normalize_secret_name(value: str) -> str:
    clean = value.strip().upper()
    if not clean:
        raise ProjectRetentionError("secret name is required")
    if not all(ch.isalnum() or ch == "_" for ch in clean):
        raise ProjectRetentionError("secret names may contain only A-Z, 0-9, and _")
    return clean


def _normalize_secret_consumer_kind(value: str) -> str:
    clean = value.strip().lower()
    if clean not in SECRET_CONSUMER_KINDS:
        raise ProjectRetentionError(f"unsupported secret consumer kind: {value}")
    return clean


def _normalize_provider(value: str) -> str:
    clean = value.strip().lower()
    if clean not in {item["id"] for item in PROVIDER_CATALOG}:
        raise ProjectRetentionError(f"unsupported provider: {value}")
    return clean


def _cap_micro(value: float | None) -> int | None:
    if value is None:
        return None
    return int(round(float(value) * 1_000_000))


class ProjectLifecycleService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def _manifest_exists(self, project_id: str) -> bool:
        try:
            validate_project_slug(project_id)
        except ValueError:
            # An invalid route ID is indistinguishable from "not found" at
            # the HTTP boundary -- callers raise ProjectNotFound (404), not
            # a validation error, for a malformed pid in a URL.
            return False
        return (
            self._workspace.root / f"{project_id}.frisket" / "manifest.json"
        ).exists()

    def _ensure_exists(self, project_id: str) -> None:
        if not self._manifest_exists(project_id):
            raise ProjectNotFound(f"no project '{project_id}'")

    def list_projects(self) -> ProjectList:
        return ProjectList.model_validate(
            [{**row, "role": None} for row in self._workspace.list()]
        )

    def create_project(
        self,
        name: str,
        *,
        sensitive: bool = False,
        idempotency_key: str | None = None,
        idempotency_scope: str | None = None,
    ) -> dict[str, Any]:
        operation_key = (idempotency_key or "").strip()
        lock_path = self._workspace.root / ".project-creation-operations.lock"
        if not operation_key:
            with FileLock(str(lock_path), timeout=-1):
                return self._workspace.create(name, sensitive=sensitive)

        actor_scope = (idempotency_scope or "").strip()
        operation_identity = (
            operation_key
            if not actor_scope
            else json.dumps(
                [actor_scope, operation_key],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        operation_digest = hashlib.sha256(
            operation_identity.encode("utf-8")
        ).hexdigest()
        operation_root = self._workspace.root / ".project-creation-operations"
        operation_path = operation_root / f"{operation_digest}.json"

        def write_operation_record(record: dict[str, Any]) -> None:
            operation_root.mkdir(parents=True, exist_ok=True)
            scratch = operation_path.with_suffix(".json.tmp")
            scratch.write_text(json.dumps(record, indent=2, sort_keys=True))
            scratch.replace(operation_path)

        def bound_bundle(
            project_id: str,
            project: dict[str, Any],
            *,
            require_creation_match: bool,
        ) -> tuple[str, dict[str, Any]]:
            manifest_path = (
                self._workspace.root / f"{project_id}.frisket" / "manifest.json"
            )
            try:
                manifest = json.loads(manifest_path.read_text())
            except (OSError, ValueError) as exc:
                raise RouteError(
                    409,
                    "project creation operation conflicts with its bound bundle",
                ) from exc
            storage_id = (
                manifest.get("storage_id") if isinstance(manifest, dict) else None
            )
            current_name = manifest.get("name") if isinstance(manifest, dict) else None
            current_description = (
                manifest.get("description", "") if isinstance(manifest, dict) else None
            )
            current_sensitive = (
                manifest.get("sensitive") if isinstance(manifest, dict) else None
            )
            current_starred = (
                manifest.get("starred", False) if isinstance(manifest, dict) else None
            )
            current_archived = (
                manifest.get("archived", False) if isinstance(manifest, dict) else None
            )
            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != "frisket-bundle"
                or manifest.get("project_id") != project_id
                or not isinstance(storage_id, str)
                or not storage_id.startswith("frisket.bundle.v1:")
                or not isinstance(current_name, str)
                or not isinstance(current_description, str)
                or not isinstance(current_sensitive, bool)
                or not isinstance(current_starred, bool)
                or not isinstance(current_archived, bool)
            ):
                raise RouteError(
                    409,
                    "project creation operation conflicts with its bound bundle",
                )
            if require_creation_match and (
                current_name != project["name"]
                or current_sensitive != project["sensitive"]
            ):
                raise RouteError(
                    409,
                    "project creation operation conflicts with its bound bundle",
                )
            return storage_id, {
                "id": project_id,
                "name": current_name,
                "description": current_description,
                "sensitive": current_sensitive,
                "starred": current_starred,
                "archived": current_archived,
            }

        with FileLock(str(lock_path), timeout=-1):
            if operation_path.exists():
                try:
                    record = json.loads(operation_path.read_text())
                    if (
                        not isinstance(record, dict)
                        or record.get("schema_version")
                        != "frisket.project_creation_operation.v3"
                        or record.get("operation_identity_sha256") != operation_digest
                        or record.get("state") not in {"pending", "complete"}
                    ):
                        raise ValueError("operation identity does not match")
                    project = record["project"]
                    if (
                        not isinstance(project, dict)
                        or not isinstance(project.get("id"), str)
                        or not isinstance(project.get("name"), str)
                        or not isinstance(project.get("description"), str)
                        or not isinstance(project.get("sensitive"), bool)
                    ):
                        raise ValueError("project binding is invalid")
                    project_id = validate_project_slug(project["id"])
                    state = record["state"]
                    recorded_storage_id = record.get("storage_id")
                    if state == "pending" and recorded_storage_id is not None:
                        raise ValueError("pending operation has a bundle identity")
                    if state == "complete" and (
                        not isinstance(recorded_storage_id, str)
                        or not recorded_storage_id.startswith("frisket.bundle.v1:")
                    ):
                        raise ValueError("completed operation has no bundle identity")
                except (KeyError, TypeError, ValueError, OSError) as exc:
                    raise RuntimeError(
                        "project creation operation record is invalid"
                    ) from exc
                if project["name"] != name or project["sensitive"] != sensitive:
                    raise RouteError(
                        409,
                        "idempotency key was already used for a different "
                        "project creation payload",
                    )

                bundle_path = self._workspace.root / f"{project_id}.frisket"
                if state == "complete":
                    if not bundle_path.exists():
                        raise RouteError(
                            409,
                            "project creation operation refers to a retired bundle",
                        )
                    actual_storage_id, current_project = bound_bundle(
                        project_id,
                        project,
                        require_creation_match=False,
                    )
                    if actual_storage_id != recorded_storage_id:
                        raise RouteError(
                            409,
                            "project creation operation refers to a different bundle",
                        )
                    return current_project

                if bundle_path.exists():
                    storage_id, _current_project = bound_bundle(
                        project_id,
                        project,
                        require_creation_match=True,
                    )
                else:
                    made = self._workspace.create(
                        project["name"],
                        project_id=project_id,
                        sensitive=project["sensitive"],
                    )
                    if made["id"] != project_id:
                        raise RuntimeError(
                            "project creation operation changed identity"
                        )
                    storage_id, _current_project = bound_bundle(
                        project_id,
                        project,
                        require_creation_match=True,
                    )
                write_operation_record(
                    {
                        **record,
                        "state": "complete",
                        "storage_id": storage_id,
                    }
                )
                return dict(project)

            slug = (
                "".join(
                    char if (char.isalnum() and char.isascii()) or char in "-_" else "-"
                    for char in name.lower().strip()
                )[:64]
                or "project"
            )
            try:
                slug = validate_project_slug(slug)
            except ValueError:
                slug = validate_project_slug(f"{slug}-project")
            project_id = slug
            suffix = 1
            while (self._workspace.root / f"{project_id}.frisket").exists():
                suffix += 1
                suffix_text = f"-{suffix}"
                project_id = validate_project_slug(
                    f"{slug[: 64 - len(suffix_text)]}{suffix_text}"
                )
            project = {
                "id": project_id,
                "name": name,
                "description": "",
                "sensitive": sensitive,
            }
            record = {
                "schema_version": "frisket.project_creation_operation.v3",
                "operation_identity_sha256": operation_digest,
                "state": "pending",
                "project": project,
            }
            write_operation_record(record)
            made = self._workspace.create(
                name,
                project_id=project_id,
                sensitive=sensitive,
            )
            if made["id"] != project_id:
                raise RuntimeError("project creation operation changed identity")
            storage_id, _current_project = bound_bundle(
                project_id,
                project,
                require_creation_match=True,
            )
            write_operation_record(
                {
                    **record,
                    "state": "complete",
                    "storage_id": storage_id,
                }
            )
            return made

    def project_metadata(self, project_id: str) -> dict[str, Any]:
        self._ensure_exists(project_id)
        return self._workspace.get(project_id).project_metadata()

    def update_project_metadata(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        starred: bool | None = None,
        archived: bool | None = None,
    ) -> dict[str, Any]:
        self._ensure_exists(project_id)
        return self._workspace.get(project_id).set_project_metadata(
            name=name,
            description=description,
            starred=starred,
            archived=archived,
        )

    def update_project_sensitivity(
        self, project_id: str, *, sensitive: bool
    ) -> dict[str, Any]:
        self._ensure_exists(project_id)
        return self._workspace.get(project_id).set_project_sensitivity(sensitive)

    def retention_policy(self, project_id: str) -> dict[str, Any]:
        self._ensure_exists(project_id)
        return _retention_policy_response(
            self._workspace.get(project_id).retention_policy()
        )

    def update_retention_policy(
        self,
        project_id: str,
        *,
        default_evidence: str | None = None,
        pin_evidence_by_default: bool | None = None,
        no_compact: bool | None = None,
    ) -> dict[str, Any]:
        self._ensure_exists(project_id)
        try:
            return _retention_policy_response(
                self._workspace.get(project_id).set_retention_policy(
                    default_evidence=default_evidence,
                    pin_evidence_by_default=pin_evidence_by_default,
                    no_compact=no_compact,
                )
            )
        except ValueError as exc:
            raise ProjectRetentionError(str(exc)) from exc

    def network_policy(self, project_id: str) -> dict[str, Any]:
        self._ensure_exists(project_id)
        return _network_policy_response(self._workspace.get(project_id))

    def update_network_policy(self, project_id: str, *, mode: str) -> dict[str, Any]:
        self._ensure_exists(project_id)
        project = self._workspace.get(project_id)
        try:
            project.set_network_policy(mode=mode)
        except ValueError as exc:
            raise ProjectNetworkPolicyError(str(exc)) from exc
        return _network_policy_response(project)

    def project_settings(self, project_id: str) -> dict[str, Any]:
        self._ensure_exists(project_id)
        return _project_settings_response(self._workspace.get(project_id))

    def update_project_settings(
        self, project_id: str, *, patch: dict[str, Any]
    ) -> dict[str, Any]:
        self._ensure_exists(project_id)
        project = self._workspace.get(project_id)
        if "media_allow_private_hosts" in patch and private_hosts_locked():
            raise ProjectSettingsError(
                "media_allow_private_hosts is locked off by the server "
                f"({MEDIA_PRIVATE_HOSTS_ENV}=deny-locked)"
            )
        try:
            patch_project_settings(project, patch)
        except ValidationError as exc:
            raise ProjectSettingsError(str(exc)) from exc
        return _project_settings_response(project)

    def compact_project(self, project_id: str) -> dict[str, Any]:
        self._ensure_exists(project_id)
        return self._workspace.get(project_id).compact()

    def provider_catalog(self) -> dict[str, Any]:
        return {
            "schemaVersion": "frisket.provider_catalog.v1",
            "providers": PROVIDER_CATALOG,
        }

    def project_provider_keys(self, project_id: str) -> dict[str, Any]:
        self._ensure_exists(project_id)
        project = self._workspace.get(project_id)
        rows = project.provider_key_catalog_rows()
        providers = []
        for provider in PROVIDER_CATALOG:
            row = rows.get(provider["id"])
            providers.append(
                {
                    **provider,
                    "configured": row is not None,
                    "hint": row["hint"] if row else None,
                    "spend_cap_usd": (
                        None
                        if row is None or row["spend_cap_micro"] is None
                        else row["spend_cap_micro"] / 1_000_000
                    ),
                    # Accrued spend on THIS key. No configured key means no
                    # spend has been made through one, so 0 is the truth
                    # there — unlike `unmetered_calls`, which says how much
                    # of the number below is missing.
                    "spent_usd": 0 if row is None else row["spent_micro"] / 1_000_000,
                    # Live calls on this key with no published price. When
                    # non-zero, spent_usd is a LOWER BOUND and the UI must
                    # not present it as the total.
                    "unmetered_calls": 0 if row is None else row["unmetered_calls"],
                    "updated_at": row["updated_at"] if row else None,
                }
            )
        return {
            "schemaVersion": "frisket.project_provider_keys.v1",
            "projectId": project_id,
            "providers": providers,
        }

    def set_project_provider_key(
        self,
        project_id: str,
        *,
        provider: str,
        key: str,
        spend_cap_usd: float | None = None,
        validation_token: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_exists(project_id)
        normalized_provider = _normalize_provider(provider)
        if not key:
            raise ProjectRetentionError("provider key is required")
        try:
            provider_config.require_validation_token(
                normalized_provider,
                key,
                validation_token,
            )
        except ValueError as exc:
            raise ProjectRetentionError(str(exc)) from exc
        project = self._workspace.get(project_id)
        project.set_provider_key(
            provider=normalized_provider,
            encrypted=encrypt_secret(key),
            hint=key_hint(key),
            spend_cap_micro=_cap_micro(spend_cap_usd),
        )
        return self.project_provider_keys(project_id)

    def validate_project_provider_key(
        self,
        project_id: str,
        *,
        provider: str,
        key: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_exists(project_id)
        normalized_provider = _normalize_provider(provider)
        candidate = (key or "").strip()
        if not candidate:
            candidate = (
                self._workspace.get(project_id)
                .provider_model_keys()
                .get(
                    normalized_provider,
                    "",
                )
            )
        if not candidate:
            return {
                "provider": normalized_provider,
                "ok": False,
                "reachable": False,
                "status": None,
                "detail": "no key configured for this provider",
            }
        result = provider_config.probe_provider(normalized_provider, candidate)
        if key and result.get("ok") is True:
            result = {
                **result,
                "validation_token": provider_config.issue_validation_token(
                    normalized_provider,
                    key,
                ),
            }
        return result

    def delete_project_provider_key(
        self, project_id: str, *, provider: str
    ) -> dict[str, Any]:
        self._ensure_exists(project_id)
        normalized_provider = _normalize_provider(provider)
        project = self._workspace.get(project_id)
        return {
            "ok": True,
            "deleted": project.delete_provider_key(normalized_provider),
            "provider": normalized_provider,
        }

    def project_secrets(self, project_id: str) -> dict[str, Any]:
        self._ensure_exists(project_id)
        project = self._workspace.get(project_id)
        rows = project.secret_rows()
        conflicts = project.secret_migration_conflict_rows()
        consumers_by_secret: dict[str, list[dict[str, str]]] = {}
        for row in project.secret_consumer_rows():
            consumers_by_secret.setdefault(str(row["name"]), []).append(
                {"kind": str(row["kind"]), "id": str(row["consumer_id"])}
            )
        runtime = workbench_plugin_runtime_index(project, project_id=project_id)
        for plugin in runtime.get("plugins", []):
            plugin_id = str(plugin.get("pluginId") or "")
            try:
                secret_names = plugin.get("requires", {}).get("secrets", [])
                for secret_name in secret_names:
                    name = _normalize_secret_name(str(secret_name))
                    consumer = {"kind": "plugin", "id": plugin_id}
                    consumers = consumers_by_secret.setdefault(name, [])
                    if consumer not in consumers:
                        consumers.append(consumer)
            except (AttributeError, TypeError):
                # A malformed requires.secrets shape on one plugin (e.g. not a
                # dict/list) must not blank the consumers of every other
                # plugin in the response.
                LOG.warning(
                    "project_secrets: plugin %r has a malformed requires.secrets"
                    " structure; skipping its secret consumers",
                    plugin_id,
                    extra={
                        "event": "project_secrets_malformed_requires_secrets",
                        "plugin_id": plugin_id,
                    },
                )
        return {
            "schemaVersion": "frisket.project_secrets.v1",
            "projectId": project_id,
            "secrets": [
                {
                    "name": row["name"],
                    "hint": row["hint"],
                    "configured": True,
                    "updatedAt": row["updated_at"],
                    "consumers": consumers_by_secret.get(str(row["name"]), []),
                }
                for row in rows
            ],
            "conflicts": [dict(row) for row in conflicts],
        }

    def set_project_secret(
        self, project_id: str, *, name: str, value: str
    ) -> dict[str, Any]:
        self._ensure_exists(project_id)
        normalized_name = _normalize_secret_name(name)
        if not value:
            raise ProjectRetentionError("secret value is required")
        project = self._workspace.get(project_id)
        project.set_secret(
            name=normalized_name,
            encrypted=encrypt_secret(value),
            hint=key_hint(value),
        )
        return self.project_secrets(project_id)

    def declare_project_secret_consumer(
        self,
        project_id: str,
        *,
        kind: str,
        consumer_id: str,
        name: str,
    ) -> dict[str, str]:
        self._ensure_exists(project_id)
        normalized_kind = _normalize_secret_consumer_kind(kind)
        normalized_name = _normalize_secret_name(name)
        clean_consumer_id = consumer_id.strip()
        if not clean_consumer_id:
            raise ProjectRetentionError("secret consumer id is required")
        project = self._workspace.get(project_id)
        project.add_secret_consumer(
            kind=normalized_kind,
            consumer_id=clean_consumer_id,
            name=normalized_name,
        )
        return {
            "kind": normalized_kind,
            "id": clean_consumer_id,
            "name": normalized_name,
        }

    def resolve_project_secret_for_consumer(
        self,
        project_id: str,
        *,
        kind: str,
        consumer_id: str,
        name: str,
    ) -> str | None:
        self._ensure_exists(project_id)
        normalized_kind = _normalize_secret_consumer_kind(kind)
        normalized_name = _normalize_secret_name(name)
        clean_consumer_id = consumer_id.strip()
        if not clean_consumer_id:
            raise ProjectRetentionError("secret consumer id is required")
        project = self._workspace.get(project_id)
        declared = project.secret_consumer_is_declared(
            kind=normalized_kind,
            consumer_id=clean_consumer_id,
            name=normalized_name,
        )
        if not declared and normalized_kind == "plugin":
            runtime = workbench_plugin_runtime_index(project, project_id=project_id)
            for plugin in runtime.get("plugins", []):
                if str(plugin.get("pluginId") or "") != clean_consumer_id:
                    continue
                declared_names = {
                    _normalize_secret_name(str(secret_name))
                    for secret_name in plugin.get("requires", {}).get("secrets", [])
                }
                if normalized_name in declared_names:
                    declared = True
                    break
        if not declared:
            raise ProjectRetentionError(
                "secret consumer is not declared for this project secret"
            )
        plaintext = project.secret_plaintext(normalized_name)
        if plaintext is None:
            if self._workspace.project_secret_fallback_resolver is None:
                return None
            return self._workspace.project_secret_fallback_resolver(
                project_id, normalized_name
            )
        return plaintext

    def delete_project_secret(self, project_id: str, *, name: str) -> dict[str, Any]:
        self._ensure_exists(project_id)
        normalized_name = _normalize_secret_name(name)
        project = self._workspace.get(project_id)
        return {
            "ok": True,
            "deleted": project.delete_secret(normalized_name),
            "name": normalized_name,
        }

    def _project_has_runs_in_flight(self, project_id: str) -> bool:
        # Handler authority lives independently of job status across
        # cooperative cancellation and lease recovery. Never remove a project
        # while executable code may still own it.
        if self._workspace.queue.has_active_project_handler(
            project_id,
            storage_org_id=self._workspace.queue_storage_org_id,
        ):
            return True
        # Queue rows cover unclaimed work plus ordinary live claims. The
        # in-process active_runs map is empty after a restart.
        return any(
            self._workspace.queue.list_project_jobs(
                project_id,
                storage_org_id=self._workspace.queue_storage_org_id,
                status=status,
                limit=1,
            )
            for status in ("queued", "running")
        )

    def _cancel_unstarted_project_jobs(self, project_id: str) -> None:
        """Cancel queued work that no handler has ever claimed.

        A local install may have no worker, so requiring the user to cancel an
        unstarted row before deleting a project can make deletion impossible.
        ``cancel_unstarted`` checks both queue status and attempt count in one
        write transaction; a claim or lease recovery makes it refuse.
        """
        while True:
            queued = self._workspace.queue.list_project_jobs(
                project_id,
                storage_org_id=self._workspace.queue_storage_org_id,
                status="queued",
                limit=100,
            )
            unstarted = [job for job in queued if job.attempts == 0]
            if not unstarted:
                return
            cancelled = sum(
                self._workspace.queue.cancel_unstarted(job.id) for job in unstarted
            )
            if cancelled == 0:
                return

    def delete_project(self, project_id: str, *, confirm_name: str) -> dict[str, Any]:
        self._ensure_exists(project_id)
        # The typed-back project name is the danger-zone confirmation, checked
        # here on the server so a client that skips or fakes the UI gate still
        # cannot delete without naming the project. Compared against the stored
        # display name, both trimmed of surrounding whitespace.
        actual_name = self._workspace.get(project_id).project_metadata()["name"]
        if confirm_name.strip() != str(actual_name).strip():
            raise ProjectDeletionRequiresConfirmation(
                "project deletion is irreversible (the bundle, its blobs and "
                "op log are removed from disk) — type the project name exactly "
                "to confirm"
            )
        self._cancel_unstarted_project_jobs(project_id)
        if self._project_has_runs_in_flight(project_id):
            raise ProjectDeletionBlocked(
                "cannot delete this project while it has runs in flight — wait "
                "for them to finish or cancel them, then try again"
            )
        self._workspace.delete(project_id)
        return {"ok": True, "deleted": project_id}

    def list_sheets(self, project_id: str) -> list[dict[str, Any]]:
        self._ensure_exists(project_id)
        project = self._workspace.get(project_id)
        sheets = project.sheets()
        # Lazy staleness (no push hooks); fast-paths root-only projects to {}.
        sync_states = compute_sync_states(project)
        # Parent-op kind/label carry "how it's made" so the frontend can drop its
        # hardcoded viaAction:'derive'. Store-owned reads keep this SQL-free.
        ops_by_id = project.ops_meta(
            [int(s["parent_op_id"]) for s in sheets if s["parent_op_id"] is not None]
        )
        out: list[dict[str, Any]] = []
        for sheet in sheets:
            sheet_id = int(sheet["id"])
            entry: dict[str, Any] = {
                "id": sheet_id,
                "name": sheet["name"],
                "parent_sheet_id": sheet["parent_sheet_id"],
                "parent_op_id": sheet["parent_op_id"],
                "rows": project.row_count(sheet_id),
                # Explicit row-title override, or None when unset (the
                # frontend computes the grid-order default).
                "title_column_id": sheet["title_column_id"],
            }
            parent_op = ops_by_id.get(
                int(sheet["parent_op_id"]) if sheet["parent_op_id"] is not None else -1
            )
            if parent_op is not None:
                entry["op_kind"] = parent_op["kind"]
                entry["op_label"] = parent_op["label"]
            # Sheet-shape signal: only edge/join-materialized sheets carry a
            # graph-availability kind.
            materialized_kind = sheet_materialized_kind(
                project,
                sheet_id,
                op_kind=parent_op["kind"] if parent_op is not None else None,
            )
            if materialized_kind is not None:
                entry["materialized_kind"] = materialized_kind
            # ALWAYS present (possibly []), unlike materialized_kind's
            # conditional-absent shape above — the regression guard is "zero
            # evidence links reports []", not "the key is missing".
            entry["cited_column_ids"] = sheet_cited_column_ids(project, sheet_id)
            # Same always-present contract as cited_column_ids above, and the
            # same reason: "no annotated text columns" must read as [], not as a
            # missing key the client has to guess about.
            entry["annotated_text_column_ids"] = annotated_text_column_ids(
                project, sheet_id
            )
            entry["dependent_sheet_ids"] = [
                item["id"] for item in project.dependent_sheets(sheet_id)
            ]
            state = sync_states.get(sheet_id)
            if state is not None:
                entry["syncState"] = state["sync_state"]
                entry["stale_reason"] = state["stale_reason"]
            out.append(entry)
        return out

    def delete_sheet(self, project_id: str, sheet_id: int) -> dict[str, Any]:
        self._ensure_exists(project_id)
        try:
            return self._workspace.get(project_id).delete_sheet(sheet_id)
        except SheetDeleteNotFound as exc:
            raise ProjectNotFound(str(exc)) from exc
        except SheetDeleteBlocked as exc:
            raise SheetDeletionBlocked(str(exc)) from exc

    def update_sheet_metadata(
        self,
        project_id: str,
        sheet_id: int,
        *,
        title_column_id: int | None,
        title_column_id_provided: bool,
    ) -> dict[str, Any]:
        """sheet-title-column-v1: PATCH /sheets/{sheet_id}. `_provided`
        distinguishes an omitted field (leave the override untouched) from an
        explicit `null` (clear it) — the ViewPatch partial-update idiom."""
        self._ensure_exists(project_id)
        project = self._workspace.get(project_id)
        try:
            require_visible_sheet(project, sheet_id)
        except RouteError as exc:
            raise KeyError(f"no sheet {sheet_id}") from exc
        if title_column_id_provided:
            project.set_sheet_title_column(sheet_id, title_column_id)
        sheet = next(
            (s for s in project.sheets() if int(s["id"]) == sheet_id),
            None,
        )
        if sheet is None:
            raise KeyError(f"no sheet {sheet_id}")
        return {"id": int(sheet["id"]), "title_column_id": sheet["title_column_id"]}

    def lineage(self, project_id: str) -> dict[str, Any]:
        """The provenance DAG for the Monitor Lineage tab (Workbench IA inc 7).

        A dedicated PRODUCT endpoint (``/debug`` stays debug-only): three tiers,
        sources -> sheets -> AI columns, with stale sheet nodes/edges flagged from
        the lazy staleness resolver. The projection lives in ``store.lineage`` so
        this service stays SQL-free.
        """
        self._ensure_exists(project_id)
        project = self._workspace.get(project_id)
        return {"project_id": project_id, **build_lineage_dag(project)}
