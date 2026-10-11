"""Secret-free local port for account-authorized Cloud execution.

Media and LLM adapters receive this port after a routed attempt has already
fixed its payer.  They never receive the device bearer or Cloud origin and
cannot choose another account, lane, provider, or route revision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Literal, Protocol, TypeAlias, runtime_checkable

from frisket.contracts.http.cloud_connection import (
    CloudDocumentConvertOptions,
    CloudExecutionResponse,
    CloudLLMRequest,
    CloudLane,
    CloudOCROptions,
    CloudQuoteChallenge,
)
from frisket.contracts.transcription_sidecar import TranscriptionOptions


CloudMediaCapability = Literal["ocr", "transcribe", "document.convert"]
CloudMediaOptions: TypeAlias = (
    CloudOCROptions | TranscriptionOptions | CloudDocumentConvertOptions
)


@dataclass(frozen=True, slots=True)
class CloudPayerSubject:
    """The exact Cloud payer and route admitted before effect."""

    user_id: int
    account_id: int
    lane: CloudLane
    provider: str
    engine: str
    route_revision: str

    def __post_init__(self) -> None:
        if isinstance(self.user_id, bool) or self.user_id < 1:
            raise ValueError("Cloud payer user_id must be a positive integer")
        if isinstance(self.account_id, bool) or self.account_id < 1:
            raise ValueError("Cloud payer account_id must be a positive integer")
        for name in ("provider", "engine", "route_revision"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"Cloud payer {name} must be a clean string")


@dataclass(frozen=True, slots=True)
class CloudExecutionAttempt:
    """Durable request identity plus the authority fixed by local admission.

    ``request_id`` is persisted before the first POST and reused for status
    recovery.  ``may_confirm_quote`` is true only when the existing action or
    Ask approval covers this exact route and spend; the transport must never
    infer it merely because Cloud returned a quote challenge.
    """

    request_id: str
    payer: CloudPayerSubject
    may_confirm_quote: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request_id, str)
            or not self.request_id
            or self.request_id != self.request_id.strip()
        ):
            raise ValueError("Cloud request_id must be a clean string")


@dataclass(frozen=True, slots=True)
class CloudMediaUpload:
    """One already-open upload without exposing its local path to Cloud."""

    file_name: str
    content_type: str
    stream: BinaryIO

    def __post_init__(self) -> None:
        if (
            not isinstance(self.file_name, str)
            or not self.file_name
            or self.file_name in {".", ".."}
            or "/" in self.file_name
            or "\\" in self.file_name
            or self.file_name != self.file_name.strip()
        ):
            raise ValueError("Cloud upload file_name must be a plain file name")
        if (
            not isinstance(self.content_type, str)
            or not self.content_type
            or self.content_type != self.content_type.strip()
            or "\r" in self.content_type
            or "\n" in self.content_type
        ):
            raise ValueError("Cloud upload content_type must be a clean string")
        if not callable(getattr(self.stream, "read", None)):
            raise TypeError("Cloud upload stream must be readable")


class CloudExecutionRefused(RuntimeError):
    """A typed pre-effect Cloud refusal safe to surface locally."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class CloudConfirmationRequired(CloudExecutionRefused):
    """Cloud quoted exact terms that current local authority cannot confirm."""

    def __init__(self, challenge: CloudQuoteChallenge) -> None:
        super().__init__("needs_confirmation", "Cloud execution needs confirmation")
        self.challenge = challenge


@runtime_checkable
class CloudExecutionTransport(Protocol):
    """Execute or recover one fixed Cloud purchase.

    Implementations own bearer authentication, exact multipart/JSON wire
    encoding, quote echo, bounded status polling, and GET recovery.  A POST
    with an uncertain outcome is recovered with ``request_id``; it is never
    changed into a fresh purchase.
    """

    async def execute_llm(
        self,
        attempt: CloudExecutionAttempt,
        request: CloudLLMRequest,
    ) -> CloudExecutionResponse: ...

    async def execute_media(
        self,
        attempt: CloudExecutionAttempt,
        *,
        capability: CloudMediaCapability,
        engine: str,
        options: CloudMediaOptions,
        upload: CloudMediaUpload,
    ) -> CloudExecutionResponse: ...

    async def recover(
        self, attempt: CloudExecutionAttempt
    ) -> CloudExecutionResponse: ...

    async def cancel(
        self, attempt: CloudExecutionAttempt
    ) -> CloudExecutionResponse: ...
