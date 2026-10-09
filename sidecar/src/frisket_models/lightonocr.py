"""Native Hugging Face adapter for the Qwen3.5 LightOnOCR-3 models."""

from __future__ import annotations

import io
import re
import threading
from dataclasses import dataclass
from typing import Any

from frisket_models import pdfium_lock
from frisket_models.errors import InvalidDocumentError
from frisket_models.markdown_plain import markdown_to_plain_text


MAX_NEW_TOKENS = 8192
PDF_RENDER_DPI = 400
MAX_IMAGE_EDGE = 2048


@dataclass(frozen=True)
class ModelProfile:
    model_id: str
    revision: str


PROFILES = {
    "lightonocr-3-0.8b": ModelProfile(
        model_id="lightonai/LightOnOCR-3-0.8B",
        revision="4a953edfc77f0e435532c503dd69dd74663d44c3",
    ),
    "lightonocr-3-4b": ModelProfile(
        model_id="lightonai/LightOnOCR-3-4B",
        revision="a06e5c5459551c9d1696468aece70ffbcf01ae62",
    ),
}

_GROUNDING_MARKER = re.compile(
    r"^[ \t]*!\[([^\]\r\n]+)\]\(([^)\r\n]*)\)[ \t]*(?:\r?\n)?",
    re.MULTILINE,
)
_NON_TEXT_BLOCK_TYPES = {"image", "chart"}


def _parse_bbox(raw: str, *, width: int, height: int) -> list[list[int]] | None:
    fields = [field.strip() for field in raw.split(",")]
    if len(fields) != 4:
        return None
    try:
        coordinates = [int(field) for field in fields]
    except ValueError:
        return None
    x1, y1, x2, y2 = coordinates
    if not all(0 <= value <= 1000 for value in coordinates):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    scaled_x1 = round(x1 * width / 1000)
    scaled_y1 = round(y1 * height / 1000)
    scaled_x2 = round(x2 * width / 1000)
    scaled_y2 = round(y2 * height / 1000)
    if scaled_x2 <= scaled_x1 or scaled_y2 <= scaled_y1:
        return None
    return [
        [scaled_x1, scaled_y1],
        [scaled_x2, scaled_y1],
        [scaled_x2, scaled_y2],
        [scaled_x1, scaled_y2],
    ]


def parse_grounding(
    raw: str, *, width: int = 1000, height: int = 1000
) -> dict[str, Any]:
    """Convert LightOnOCR grounding markers to the shared OCR page shape.

    Geometry is accepted only when all four normalized coordinates are valid,
    then projected into the original input image's pixel dimensions. Image and
    chart blocks contain generated descriptions or inferred data, not recognized
    page text, and are therefore excluded from OCR output.
    """

    if width <= 0 or height <= 0:
        raise ValueError("Grounding image dimensions must be positive")

    matches = list(_GROUNDING_MARKER.finditer(raw))
    blocks: list[dict[str, Any]] = []

    prefix_end = matches[0].start() if matches else len(raw)
    prefix = markdown_to_plain_text(raw[:prefix_end])
    if prefix:
        blocks.append({"text": prefix, "type": "text"})

    for index, marker in enumerate(matches):
        label = marker.group(1).strip().lower()
        base_label = label.removesuffix("+")
        if base_label in _NON_TEXT_BLOCK_TYPES:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        text = markdown_to_plain_text(raw[marker.end() : end])
        if not text:
            continue
        block: dict[str, Any] = {"text": text, "type": label}
        if bbox := _parse_bbox(marker.group(2), width=width, height=height):
            block["bbox"] = bbox
        blocks.append(block)

    return {
        "text": "\n".join(block["text"] for block in blocks).strip(),
        "blocks": blocks,
    }


def _select_runtime(torch_module: Any) -> tuple[str, Any]:
    if torch_module.cuda.is_available():
        dtype = (
            torch_module.bfloat16
            if torch_module.cuda.is_bf16_supported()
            else torch_module.float16
        )
        return "cuda", dtype
    if torch_module.backends.mps.is_available():
        return "mps", torch_module.float16
    return "cpu", torch_module.float32


def _sequence_length(sequence: Any) -> int:
    shape = getattr(sequence, "shape", None)
    if shape:
        return int(shape[-1])
    return len(sequence)


def _token_value(token: Any) -> int:
    if hasattr(token, "item"):
        token = token.item()
    return int(token)


def _eos_token_ids(model: Any) -> set[int]:
    result: set[int] = set()
    for owner_name in ("generation_config", "config"):
        value = getattr(getattr(model, owner_name, None), "eos_token_id", None)
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple, set)) else [value]
        result.update(int(item) for item in values)
    return result


def _looks_like_pdf(data: bytes) -> bool:
    # Match the host's page-limit sniff so no input reaches PDFium unbounded.
    return data.startswith(b"%PDF-")


def _close(resource: Any) -> None:
    close = getattr(resource, "close", None)
    if close is not None:
        close()


class LightOnOCRAdapter:
    """One resident model shared by the OCR and Markdown route views."""

    def __init__(
        self,
        *,
        engine: str,
        model: Any,
        processor: Any,
        torch_module: Any,
        device: str,
    ) -> None:
        self.engine = engine
        self._model = model
        self._processor = processor
        self._torch = torch_module
        self._device = device
        self._generation_lock = threading.Lock()

    @staticmethod
    def _prepare_image(image: Any) -> Any:
        converted = image.convert("RGB")
        if converted is not image:
            _close(image)
        converted.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
        return converted

    def _decode_image(self, data: bytes) -> tuple[Any, tuple[int, int]]:
        from PIL import Image, ImageOps

        image = None
        bomb_error = getattr(Image, "DecompressionBombError", OSError)
        try:
            image = Image.open(io.BytesIO(data))
            if int(getattr(image, "n_frames", 1)) != 1:
                raise InvalidDocumentError(
                    "LightOnOCR supports only single-frame images"
                )
            pixel_limit = getattr(Image, "MAX_IMAGE_PIXELS", None)
            if pixel_limit is not None and image.width * image.height > pixel_limit:
                raise InvalidDocumentError(
                    "image exceeds Pillow's safe decoded-pixel budget"
                )
            image.load()
            oriented = ImageOps.exif_transpose(image)
            if oriented is not image:
                _close(image)
                image = oriented
            original_size = tuple(image.size)
            return self._prepare_image(image), original_size
        except InvalidDocumentError:
            if image is not None:
                _close(image)
            raise
        except (OSError, SyntaxError, bomb_error) as exc:
            if image is not None:
                _close(image)
            raise InvalidDocumentError(
                "LightOnOCR supports PDF and single-frame image documents"
            ) from exc
        except BaseException:
            if image is not None:
                _close(image)
            raise

    def _generate(self, image: Any, prompt: str | None) -> str:
        content: list[dict[str, Any]] = [{"type": "image", "image": image}]
        if prompt is not None:
            content.append({"type": "text", "text": prompt})
        conversation = [{"role": "user", "content": content}]

        with self._generation_lock, self._torch.inference_mode():
            inputs = self._processor.apply_chat_template(
                conversation,
                add_generation_prompt=True,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
                enable_thinking=False,
            ).to(self._device)
            output_ids = self._model.generate(
                **inputs,
                max_new_tokens=MAX_NEW_TOKENS,
                do_sample=False,
            )
            prompt_length = int(inputs["input_ids"].shape[-1])
            generated_ids = output_ids[0][prompt_length:]
            generated_length = _sequence_length(generated_ids)
            if generated_length >= MAX_NEW_TOKENS:
                eos_ids = _eos_token_ids(self._model)
                final_is_eos = (
                    bool(eos_ids) and _token_value(generated_ids[-1]) in eos_ids
                )
                if not final_is_eos:
                    raise RuntimeError(
                        f"{self.engine} output reached the {MAX_NEW_TOKENS}-token limit"
                    )
            return self._processor.decode(
                generated_ids, skip_special_tokens=True
            ).strip()

    def ocr(self, images: list[bytes]) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        for data in images:
            image, (width, height) = self._decode_image(data)
            try:
                pages.append(
                    parse_grounding(
                        self._generate(image, "grounding"),
                        width=width,
                        height=height,
                    )
                )
            finally:
                _close(image)
        return pages

    def _markdown_from_pdf(self, data: bytes) -> tuple[list[str], list[bool]]:
        import pypdfium2 as pdfium

        markdown_pages: list[str] = []
        ocr_used: list[bool] = []
        pdf = None
        pdfium_error = getattr(pdfium, "PdfiumError", OSError)
        try:
            try:
                with pdfium_lock.PDFIUM_LOCK:
                    pdf = pdfium.PdfDocument(data)
                    page_count = len(pdf)
                for index in range(page_count):
                    page = None
                    bitmap = None
                    image = None
                    try:
                        with pdfium_lock.PDFIUM_LOCK:
                            page = pdf[index]
                            page_width, page_height = page.get_size()
                            longest_edge = max(float(page_width), float(page_height))
                            if longest_edge <= 0:
                                raise InvalidDocumentError(
                                    "PDF page dimensions must be positive"
                                )
                            scale = min(
                                PDF_RENDER_DPI / 72,
                                MAX_IMAGE_EDGE / longest_edge,
                            )
                            bitmap = page.render(scale=scale)
                            image = self._prepare_image(bitmap.to_pil())
                        markdown_pages.append(self._generate(image, None))
                        ocr_used.append(True)
                    finally:
                        if image is not None:
                            _close(image)
                        with pdfium_lock.PDFIUM_LOCK:
                            if bitmap is not None:
                                _close(bitmap)
                            if page is not None:
                                _close(page)
            finally:
                with pdfium_lock.PDFIUM_LOCK:
                    if pdf is not None:
                        _close(pdf)
        except pdfium_error as exc:
            raise InvalidDocumentError("LightOnOCR could not decode the PDF") from exc
        return markdown_pages, ocr_used

    def to_markdown(self, filename: str, data: bytes) -> dict[str, Any]:
        del filename  # MIME is detected from bytes, not an untrusted suffix.
        if _looks_like_pdf(data):
            markdown_pages, ocr_used = self._markdown_from_pdf(data)
        else:
            image, _original_size = self._decode_image(data)
            try:
                markdown_pages = [self._generate(image, None)]
                ocr_used = [True]
            finally:
                _close(image)
        return {"markdown": "\n\n".join(markdown_pages), "ocr_used": ocr_used}


def load_lightonocr(engine: str) -> LightOnOCRAdapter:
    """Load one pinned LightOnOCR model; weights download from HF on first use."""

    try:
        profile = PROFILES[engine]
    except KeyError as exc:
        supported = ", ".join(sorted(PROFILES))
        raise ValueError(
            f"Unsupported LightOnOCR engine {engine!r}; expected one of: {supported}"
        ) from exc

    # Heavy optional dependencies remain lazy so the base sidecar can expose a
    # truthful missing-extra capability instead of importing torch at startup.
    import torch
    from transformers import AutoProcessor, Qwen3_5ForConditionalGeneration

    device, dtype = _select_runtime(torch)
    model = Qwen3_5ForConditionalGeneration.from_pretrained(
        profile.model_id,
        revision=profile.revision,
        dtype=dtype,
        use_safetensors=True,
    )
    model.to(device)
    model.eval()
    processor = AutoProcessor.from_pretrained(
        profile.model_id,
        revision=profile.revision,
        backend="pil",
    )
    return LightOnOCRAdapter(
        engine=engine,
        model=model,
        processor=processor,
        torch_module=torch,
        device=device,
    )
