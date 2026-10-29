"""Conservative direct-PDF support shared by selection and execution."""

from frisket.ai.llm.model_catalog import MODEL_ENTRIES
from frisket.ai.llm.types import LLMError

# Product bounds leave room for base64 and the prompt in the existing transport.
MAX_PDF_INPUT_BYTES = 20 * 1024 * 1024
MAX_PDF_BYTES = MAX_PDF_INPUT_BYTES
MAX_PDF_PAGES = 100
PDF_ACTION_IDS = frozenset({"map.ask", "map.summarize", "map.extract"})


def model_supports_pdf(model: str) -> bool:
    provider, separator, model_id = model.partition("/")
    return bool(
        separator
        and provider in {"openai", "anthropic"}
        and any(
            entry["id"] == model_id and entry["vision"]
            for entry in MODEL_ENTRIES.get(provider, ())
        )
    )


def require_pdf_model(model: str) -> None:
    if not model_supports_pdf(model):
        raise LLMError(
            "This model does not support direct PDF input. Choose a supported "
            "OpenAI or Anthropic model, or convert the PDF to text first.",
            retryable=False,
            provider_code="pdf_input_unsupported",
        )


def inspect_pdf_bytes(
    data: bytes,
    *,
    max_bytes: int = MAX_PDF_INPUT_BYTES,
    max_pages: int = MAX_PDF_PAGES,
) -> tuple[int, int | None]:
    """Derive bounded PDF facts from bytes, never caller-supplied metadata."""
    from io import BytesIO

    from pypdf import PdfReader

    if len(data) > max_bytes:
        raise LLMError(
            "Direct PDF input exceeds the supported file size.",
            provider_code="pdf_input_too_large",
        )
    if not data[:1024].lstrip().startswith(b"%PDF-"):
        raise LLMError(
            "This file does not contain a valid PDF.", provider_code="pdf_input_invalid"
        )
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise LLMError(
                "Encrypted PDFs cannot be sent directly. Use an unlocked PDF.",
                provider_code="pdf_input_invalid",
            )
        count = len(reader.pages)
        if not 1 <= count <= max_pages:
            raise LLMError(
                "Direct PDF input exceeds the supported page count. Select a page or a smaller PDF.",
                provider_code="pdf_input_too_large",
            )
        try:
            text_chars = sum(len(page.extract_text() or "") for page in reader.pages)
        except Exception:  # noqa: BLE001 - optional native text estimate metadata
            text_chars = None
        return count, text_chars
    except LLMError:
        raise
    except Exception as exc:  # noqa: BLE001 - hostile document parser boundary
        raise LLMError(
            "The PDF could not be read. Check the file and selected page.",
            provider_code="pdf_input_invalid",
        ) from exc
