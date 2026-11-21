"""One owner for a provider purchase, from determination to recorded outcome.

Transport owners call this module; actions do not implement accounting hooks.
The policy is deployment composition, not a caller-selected ``free`` flag.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar

from frisket.execution.dispatch_control import DispatchControl


class PurchasePolicyRequired(RuntimeError):
    """A hosted effect has no accounting owner; no request was sent."""


class PurchaseRecordingError(RuntimeError):
    """The provider returned; retrying this error must not buy the work again."""

    post_egress_ambiguous = True
    provider_job_accepted = True

    def __init__(
        self,
        message: str,
        *,
        provider_result: Any,
        observation: Any = None,
        purchase_request: Any = None,
        purchase_admission: Any = None,
    ):
        super().__init__(message)
        self.provider_result = provider_result
        self.observation = observation
        self.purchase_request = purchase_request
        self.purchase_admission = purchase_admission


class PurchaseNotDispatched(RuntimeError):
    """Admission succeeded but execution closed before invoking the provider."""

    purchase_dispatched = False


@dataclass(frozen=True)
class PurchaseRequest:
    provider: str
    capability: str
    engine: str
    credential_source: str
    prepared: dict[str, Any]
    context: Any = field(default=None, repr=False)
    estimated_cost_usd: float | None = None
    quantity: dict[str, float | None] = field(default_factory=dict)
    llm_request: Any = field(default=None, repr=False)
    estimate: Any = field(default=None, repr=False)


@dataclass(frozen=True)
class PurchaseAdmission:
    disposition: Literal["local", "byok", "included", "metered", "relay"]
    operation_id: str | None = None

    def __post_init__(self) -> None:
        if self.disposition not in {"local", "byok", "included", "metered", "relay"}:
            raise ValueError("Unknown purchase disposition")
        if self.disposition == "metered" and not self.operation_id:
            raise ValueError("Accounted purchases require an operation identity")


@dataclass(frozen=True)
class PurchaseObservation:
    result: Any
    usage: dict[str, Any]
    provider_cost_micro: int | None = None


class PurchasePolicy(Protocol):
    def prepare(self, request: Any, credential_source: str) -> Any: ...

    def admit_purchase(self, request: PurchaseRequest) -> PurchaseAdmission: ...

    def record_purchase(
        self,
        request: PurchaseRequest,
        admission: PurchaseAdmission,
        observation: PurchaseObservation,
    ) -> None: ...

    def fail_purchase(
        self,
        request: PurchaseRequest,
        admission: PurchaseAdmission,
        error: BaseException,
    ) -> None: ...


def cloud_context(context: Any) -> bool:
    return (
        getattr(
            getattr(context, "credential_use_context", None), "deployment_owner", None
        )
        is not None
    )


class LocalPurchasePolicy:
    """Local/operator-owned work has no Frisket charge, explicitly.

    This policy cannot authorize use of platform credentials. Cloud composition
    must provide its own policy, including for deliberately included engines.
    """

    def prepare(self, request: Any, credential_source: str) -> Any:
        self._require_local(credential_source)
        return request

    @staticmethod
    def _require_local(source: str) -> None:
        if source not in {"none", "local", "project_key", "org_byok", "cache"}:
            raise PurchasePolicyRequired(
                "Platform execution requires a purchase policy"
            )

    def admit_purchase(self, request: PurchaseRequest) -> PurchaseAdmission:
        self._require_local(request.credential_source)
        if request.provider == "frisket-cloud":
            from frisket.cloud_execution import cloud_llm_execution_bound

            if not cloud_llm_execution_bound(request.engine):
                raise PurchasePolicyRequired("Cloud relay requires its admitted route")
            return PurchaseAdmission("relay")
        if cloud_context(request.context):
            raise PurchasePolicyRequired("Hosted execution requires a purchase policy")
        return PurchaseAdmission(
            "byok"
            if request.credential_source in {"local", "project_key", "org_byok"}
            and request.provider not in {"local", "ollama", "frisket-models"}
            else "local"
        )

    def record_purchase(self, request, admission, observation) -> None:
        pass

    def fail_purchase(self, request, admission, error) -> None:
        pass


LOCAL_PURCHASE_POLICY = LocalPurchasePolicy()


def policy_for_context(context: Any) -> PurchasePolicy:
    """Resolve composition once at the transport entrance, never infer free Cloud."""
    extras = getattr(context, "extras", None) or {}
    policy = extras.get("service_operation_policy")
    if policy is None:
        policy = getattr(extras.get("router"), "model_call_policy", None)
    if policy is None:
        if cloud_context(context):
            raise PurchasePolicyRequired("Hosted execution requires a purchase policy")
        return LOCAL_PURCHASE_POLICY
    if not callable(getattr(policy, "admit_purchase", None)):
        raise PurchasePolicyRequired("Execution purchase policy is incomplete")
    return policy


def cost_micro(cost: Any) -> int | None:
    """Unknown or malformed provider cost is never silently zero."""
    import math

    if (
        isinstance(cost, (int, float))
        and not isinstance(cost, bool)
        and math.isfinite(cost)
        and cost >= 0
    ):
        return math.ceil(cost * 1_000_000)
    return None


def llm_observation(response: Any) -> PurchaseObservation:
    """One physical response, excluding earlier purchases carried for callers."""
    from dataclasses import asdict

    result = asdict(response)
    result.pop("prior_wire_calls", None)
    usage_available = response.usage_available
    expense = cost_micro(getattr(response, "cost", None)) if usage_available else None
    if expense is None:
        result.update(cost=None, cost_source="unknown")
    return PurchaseObservation(
        result,
        {"tokens_in": response.tokens_in, "tokens_out": response.tokens_out}
        if usage_available
        else {},
        expense,
    )


T = TypeVar("T")


class _PurchaseExecution:
    """The shared lifecycle for async network and synchronous local backends."""

    def __init__(self, request, policy, dispatch, observe, observe_error):
        if not callable(getattr(policy, "admit_purchase", None)):
            raise PurchasePolicyRequired("Execution requires a purchase policy")
        self.request, self.policy = request, policy
        self.observe, self.observe_error = observe, observe_error
        dispatch.check()
        self.admission = policy.admit_purchase(request)
        if not isinstance(self.admission, PurchaseAdmission):
            raise PurchasePolicyRequired("Purchase policy did not determine accounting")
        try:
            dispatch.check()
        except BaseException as error:
            self._fail(
                PurchaseNotDispatched("Execution closed before provider dispatch"),
                primary=error,
            )
            raise

    def _fail(self, error, *, primary=None):
        try:
            self.policy.fail_purchase(self.request, self.admission, error)
        except Exception as accounting_error:
            (
                primary if primary is not None else error
            ).purchase_recording_error = accounting_error

    def failed(self, error: BaseException) -> None:
        try:
            known = (
                self.observe_error(error) if self.observe_error is not None else None
            )
        except Exception as observation_error:
            error.purchase_observation_error = observation_error
            self._fail(error)
            return
        if known is None:
            self._fail(error)
            return
        try:
            self.policy.record_purchase(self.request, self.admission, known)
        except Exception as accounting_error:
            error.observation = known
            error.purchase_recording_error = accounting_error
            self._fail(error)

    def returned(self, result):
        observation = None
        try:
            observation = self.observe(result)
            self.policy.record_purchase(self.request, self.admission, observation)
        except BaseException as error:
            # A transport-specific observer can itself fail after a real return.
            # Recover only an understood response type, never guess arbitrary
            # page/audio/token units from a dictionary with unknown semantics.
            if observation is None:
                from frisket.ai.llm.types import LLMResponse

                if isinstance(result, LLMResponse):
                    observation = llm_observation(result)
            recording_error = PurchaseRecordingError(
                str(error),
                provider_result=result,
                observation=observation,
                purchase_request=self.request,
                purchase_admission=self.admission,
            )
            self._fail(recording_error)
            import asyncio

            if isinstance(error, asyncio.CancelledError):
                error.provider_result = result
                error.observation = observation
                error.purchase_request = self.request
                error.purchase_admission = self.admission
                raise
            raise recording_error from error
        return result


async def execute_purchase(
    request: PurchaseRequest,
    *,
    policy: PurchasePolicy,
    dispatch: DispatchControl,
    invoke: Callable[[], Awaitable[T]],
    observe: Callable[[T], PurchaseObservation],
    observe_error: Callable[[BaseException], PurchaseObservation | None] | None = None,
) -> T:
    """Own one logical purchase; recording never retries the provider.

    Cancellation closes future dispatch, not the accounting of returned work.
    Outcome extraction belongs to the transport owner and precedes action-level
    validation. A crash with no returned evidence remains an unknown outcome.
    """
    owner = _PurchaseExecution(request, policy, dispatch, observe, observe_error)
    try:
        result = await invoke()
    except BaseException as error:
        owner.failed(error)
        raise
    return owner.returned(result)


def execute_purchase_sync(
    request: PurchaseRequest,
    *,
    policy: PurchasePolicy,
    dispatch: DispatchControl,
    invoke: Callable[[], T],
    observe: Callable[[T], PurchaseObservation],
    observe_error: Callable[[BaseException], PurchaseObservation | None] | None = None,
) -> T:
    """The same owner for synchronous backend batches, without an event loop."""
    owner = _PurchaseExecution(request, policy, dispatch, observe, observe_error)
    try:
        result = invoke()
    except BaseException as error:
        owner.failed(error)
        raise
    return owner.returned(result)
