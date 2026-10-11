"""Durable per-wire Cloud LLM execution below router cache lookup."""

from __future__ import annotations

import hashlib
from typing import Any

from frisket.cloud_execution import (
    CloudExecutionAttempt,
    CloudExecutionRefused,
    CloudExecutionTransport,
    CloudPayerSubject,
    CloudQuoteAuthority,
    cloud_execution_accounting,
)
from frisket.contracts.http.cloud_connection import (
    CloudExecutionResponse,
    CloudLLMRequest,
)
from frisket.engine.store.effect_checkpoints import (
    EffectCheckpointRef,
    EffectCheckpointStore,
    canonical_json,
)

_FAMILY = "model_call"
_SCHEMA = "frisket.cloud_llm_checkpoint.v1"


CloudLLMCheckpointRef = EffectCheckpointRef


class CloudLLMCheckpointCoordinator:
    """Allocate and recover one ordered sequence of Cloud LLM wire calls.

    One coordinator belongs to one durable action unit (a map row, Ask turn,
    or research operation). The canonical final request identifies its child;
    an exact repeat inside that owner replays the same purchase. Router retries
    remain inside one call, while changed structured repairs and tool histories
    naturally produce distinct request hashes.
    """

    def __init__(
        self,
        project: Any,
        transport: CloudExecutionTransport,
        *,
        payer: CloudPayerSubject,
        quote_authority: CloudQuoteAuthority,
        group_key: str,
        action_kind: str,
        run_id: int | None = None,
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        claimless_direct_effect: bool = False,
    ) -> None:
        if not callable(getattr(transport, "execute_llm", None)):
            raise TypeError("Cloud LLM transport is invalid")
        if not isinstance(payer, CloudPayerSubject):
            raise TypeError("Cloud LLM payer is invalid")
        if not isinstance(quote_authority, CloudQuoteAuthority):
            raise TypeError("Cloud LLM quote authority is invalid")
        for name, value in (("group_key", group_key), ("action_kind", action_kind)):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"Cloud LLM {name} must be a clean string")
        self.project = project
        self.transport = transport
        self.payer = payer
        self.quote_authority = quote_authority
        self.group_key = group_key
        self.action_kind = action_kind
        self.run_id = run_id
        self.writer_attempt_id = writer_attempt_id
        self.claim_token = claim_token
        self.claimless_direct_effect = claimless_direct_effect
        self._returned: dict[str, CloudLLMCheckpointRef] = {}
        self._accounting: dict[str, dict[str, Any]] = {}

    @property
    def returned_checkpoints(self) -> tuple[CloudLLMCheckpointRef, ...]:
        return tuple(self._returned.values())

    @property
    def returned_accounting(self) -> tuple[dict[str, Any], ...]:
        """Receipt-derived facts for every returned child, keyed for dedup."""

        return tuple(self._accounting.values())

    async def execute(
        self, request: CloudLLMRequest
    ) -> tuple[CloudExecutionAttempt, CloudExecutionResponse]:
        """Execute or replay one logical wire round with one durable key."""

        if not isinstance(request, CloudLLMRequest):
            raise TypeError("Cloud LLM request is invalid")
        request_payload = request.model_dump(mode="json", by_alias=True)
        unit_key = hashlib.sha256(
            canonical_json(request_payload).encode("utf-8")
        ).hexdigest()
        logical = {
            "schema_version": _SCHEMA,
            "group_key": self.group_key,
            "unit_key": unit_key,
        }
        checkpoint_id = (
            "cloud_llm_"
            + hashlib.sha256(canonical_json(logical).encode("utf-8")).hexdigest()
        )
        identity_payload = {
            **logical,
            "action_kind": self.action_kind,
            "payer": self.payer.to_snapshot(),
            "request": request_payload,
        }
        identity = hashlib.sha256(
            canonical_json(identity_payload).encode("utf-8")
        ).hexdigest()
        store = EffectCheckpointStore(self.project.db)
        payload = {
            "schema_version": _SCHEMA,
            "payer": self.payer.to_snapshot(),
            "request": request_payload,
            "response": None,
        }
        created = store.reserve(
            checkpoint_id,
            family=_FAMILY,
            group_key=self.group_key,
            unit_key=unit_key,
            action_kind=self.action_kind,
            identity=identity,
            authorized_attempt_id=self.writer_attempt_id,
            payload=payload,
            run_id=self.run_id,
            writer_attempt_id=self.writer_attempt_id,
            claim_token=self.claim_token,
            claimless_direct_effect=self.claimless_direct_effect,
        )
        row = store.find_unit(
            family=_FAMILY, group_key=self.group_key, unit_key=unit_key
        )
        if row is None:
            raise CloudExecutionRefused(
                "external_effect_reconciliation_required",
                "Cloud LLM checkpoint disappeared before execution",
            )
        self._verify_row(row, identity=identity, payload=payload)
        attempt = CloudExecutionAttempt(
            request_id=checkpoint_id,
            payer=self.payer,
            quote_authority=self._remaining_authority(store, unit_key),
        )
        if not created and row["state"] == "returned":
            response = self._response(row)
            self._remember(checkpoint_id, unit_key, identity, attempt, response)
            return attempt, response
        if row["state"] != "reserved":
            raise CloudExecutionRefused(
                "external_effect_reconciliation_required",
                "Cloud LLM checkpoint has an invalid state",
            )

        # A reserved Cloud POST is deliberately replayable: Cloud binds the
        # idempotency key to this exact user/account/body and never rebills it.
        response = await self.transport.execute_llm(attempt, request)
        returned_payload = {**payload, "response": response.model_dump(mode="json")}
        store.complete(
            checkpoint_id,
            family=_FAMILY,
            group_key=self.group_key,
            unit_key=unit_key,
            action_kind=self.action_kind,
            identity=identity,
            payload=returned_payload,
            run_id=self.run_id,
            writer_attempt_id=self.writer_attempt_id,
            claim_token=self.claim_token,
            claimless_direct_effect=self.claimless_direct_effect,
        )
        self._remember(checkpoint_id, unit_key, identity, attempt, response)
        return attempt, response

    def retire_returned(self) -> None:
        """Retire children after the owner durably recorded their accounting."""

        store = EffectCheckpointStore(self.project.db)
        for ref in self.returned_checkpoints:
            store.consume_and_retire(
                ref.checkpoint_id,
                family=_FAMILY,
                group_key=ref.group_key,
                unit_key=ref.unit_key,
                action_kind=ref.action_kind,
                identity=ref.identity,
                run_id=self.run_id,
                writer_attempt_id=self.writer_attempt_id,
                claim_token=self.claim_token,
                claimless_direct_effect=self.claimless_direct_effect,
            )
            self._returned.pop(ref.checkpoint_id, None)
            self._accounting.pop(ref.checkpoint_id, None)

    def _remaining_authority(
        self, store: EffectCheckpointStore, unit_key: str
    ) -> CloudQuoteAuthority:
        ceiling = self.quote_authority.maximum_charge_micro
        if ceiling is None:
            return self.quote_authority
        spent = 0
        for row in store.list_group(family=_FAMILY, group_key=self.group_key):
            if row.get("unit_key") == unit_key or row["state"] != "returned":
                continue
            response = self._response(row)
            prior_attempt = CloudExecutionAttempt(
                request_id=str(row["id"]),
                payer=self.payer,
                quote_authority=None,
            )
            accounting = cloud_execution_accounting(
                prior_attempt, response, capability="llm.complete"
            )
            prior_cost = accounting["model_calls"][0]["units"].get(
                "cloud_effective_cost_micro"
            )
            if isinstance(prior_cost, bool) or not isinstance(prior_cost, int):
                raise CloudExecutionRefused(
                    "external_effect_reconciliation_required",
                    "Cloud LLM checkpoint has invalid spend accounting",
                )
            spent += prior_cost
        if spent > ceiling:
            raise CloudExecutionRefused(
                "cloud_quote_exhausted",
                "Prior Cloud LLM calls exhausted the admitted allowance",
            )
        return CloudQuoteAuthority(maximum_charge_micro=ceiling - spent)

    def _verify_row(
        self,
        row: dict[str, Any],
        *,
        identity: str,
        payload: dict[str, Any],
    ) -> None:
        if (
            row.get("action_kind") != self.action_kind
            or row.get("identity") != identity
        ):
            raise CloudExecutionRefused(
                "external_effect_reconciliation_required",
                "Cloud LLM checkpoint belongs to a different request or payer",
            )
        stored = row.get("payload")
        if not isinstance(stored, dict) or any(
            stored.get(key) != payload[key]
            for key in ("schema_version", "payer", "request")
        ):
            raise CloudExecutionRefused(
                "external_effect_reconciliation_required",
                "Cloud LLM checkpoint payload is incomplete or corrupt",
            )

    @staticmethod
    def _response(row: dict[str, Any]) -> CloudExecutionResponse:
        payload = row.get("payload")
        response = payload.get("response") if isinstance(payload, dict) else None
        try:
            return CloudExecutionResponse.model_validate(response)
        except (TypeError, ValueError) as exc:
            raise CloudExecutionRefused(
                "external_effect_reconciliation_required",
                "Cloud LLM checkpoint response is incomplete or corrupt",
            ) from exc

    def _remember(
        self,
        checkpoint_id: str,
        unit_key: str,
        identity: str,
        attempt: CloudExecutionAttempt,
        response: CloudExecutionResponse,
    ) -> None:
        self._returned[checkpoint_id] = CloudLLMCheckpointRef(
            checkpoint_id=checkpoint_id,
            group_key=self.group_key,
            unit_key=unit_key,
            action_kind=self.action_kind,
            identity=identity,
        )
        self._accounting[checkpoint_id] = cloud_execution_accounting(
            attempt, response, capability="llm.complete"
        )


__all__ = ["CloudLLMCheckpointCoordinator", "CloudLLMCheckpointRef"]
