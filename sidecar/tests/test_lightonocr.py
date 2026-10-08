from __future__ import annotations

import contextlib
import sys
from types import SimpleNamespace

import pytest

from frisket_models import lightonocr


class _Batch(dict):
    def __init__(self, calls: dict[str, object], prompt_length: int = 2) -> None:
        super().__init__(input_ids=SimpleNamespace(shape=(1, prompt_length)))
        self._calls = calls

    def to(self, device: str) -> "_Batch":
        self._calls["batch_device"] = device
        return self


class _Processor:
    def __init__(self, calls: dict[str, object], decoded: list[str]) -> None:
        self._calls = calls
        self._decoded = decoded

    def apply_chat_template(
        self, conversation: list[dict], **options: object
    ) -> _Batch:
        self._calls.setdefault("conversations", []).append(conversation)
        self._calls.setdefault("template_options", []).append(options)
        return _Batch(self._calls)

    def decode(self, token_ids: object, **options: object) -> str:
        self._calls.setdefault("decoded_tokens", []).append(token_ids)
        self._calls.setdefault("decode_options", []).append(options)
        return self._decoded.pop(0)


class _Model:
    def __init__(self, calls: dict[str, object], generated_tokens: int = 2) -> None:
        self._calls = calls
        self._generated_tokens = generated_tokens
        self.device = "unplaced"
        self.generation_config = SimpleNamespace(eos_token_id=None)

    def to(self, device: str) -> "_Model":
        self.device = device
        self._calls["model_device"] = device
        return self

    def eval(self) -> "_Model":
        self._calls["eval"] = True
        return self

    def generate(self, **options: object) -> list[list[int]]:
        self._calls.setdefault("generate_options", []).append(options)
        return [[10, 11, *range(100, 100 + self._generated_tokens)]]


class _Image:
    def __init__(
        self,
        name: str,
        events: list[object],
        size: tuple[int, int] = (1000, 1000),
    ) -> None:
        self.name = name
        self.events = events
        self.size = size

    def load(self) -> None:
        self.events.append(("load", self.name))

    def convert(self, mode: str) -> "_Image":
        self.events.append(("convert", self.name, mode))
        return self

    def thumbnail(self, size: tuple[int, int]) -> None:
        self.events.append(("thumbnail", self.name, size))
        ratio = min(1, size[0] / self.size[0], size[1] / self.size[1])
        self.size = (round(self.size[0] * ratio), round(self.size[1] * ratio))

    def close(self) -> None:
        self.events.append(("image-close", self.name))


def _fake_torch(*, cuda: bool = False, mps: bool = False, bf16: bool = False):
    return SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=lambda: cuda,
            is_bf16_supported=lambda: bf16,
        ),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: mps)),
        bfloat16="bfloat16",
        float16="float16",
        float32="float32",
        inference_mode=contextlib.nullcontext,
    )


@pytest.mark.parametrize(
    ("engine", "model_id", "revision"),
    [
        (
            "lightonocr-3-0.8b",
            "lightonai/LightOnOCR-3-0.8B",
            "4a953edfc77f0e435532c503dd69dd74663d44c3",
        ),
        (
            "lightonocr-3-4b",
            "lightonai/LightOnOCR-3-4B",
            "a06e5c5459551c9d1696468aece70ffbcf01ae62",
        ),
    ],
)
def test_load_uses_pinned_native_qwen_profiles(
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    model_id: str,
    revision: str,
) -> None:
    calls: dict[str, object] = {}
    model = _Model(calls)
    processor = _Processor(calls, [])

    class Qwen:
        @staticmethod
        def from_pretrained(model_id: str, **options: object) -> _Model:
            calls["model_load"] = (model_id, options)
            return model

    class AutoProcessor:
        @staticmethod
        def from_pretrained(model_id: str, **options: object) -> _Processor:
            calls["processor_load"] = (model_id, options)
            return processor

    monkeypatch.setitem(sys.modules, "torch", _fake_torch())
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(
            AutoProcessor=AutoProcessor,
            Qwen3_5ForConditionalGeneration=Qwen,
        ),
    )

    adapter = lightonocr.load_lightonocr(engine)

    assert adapter.engine == engine
    assert calls["model_load"] == (
        model_id,
        {"revision": revision, "dtype": "float32", "use_safetensors": True},
    )
    assert calls["processor_load"] == (
        model_id,
        {"revision": revision, "backend": "pil"},
    )
    assert "trust_remote_code" not in calls["model_load"][1]
    assert calls["model_device"] == "cpu"
    assert calls["eval"] is True


def test_load_rejects_other_lightonocr_architectures() -> None:
    with pytest.raises(ValueError, match="Unsupported LightOnOCR engine"):
        lightonocr.load_lightonocr("lightonocr-3-1b")


@pytest.mark.parametrize(
    ("torch_module", "expected"),
    [
        (_fake_torch(cuda=True, bf16=True), ("cuda", "bfloat16")),
        (_fake_torch(cuda=True), ("cuda", "float16")),
        (_fake_torch(mps=True), ("mps", "float16")),
        (_fake_torch(), ("cpu", "float32")),
    ],
)
def test_runtime_selection_uses_supported_device_dtype(
    torch_module: object, expected: tuple[str, str]
) -> None:
    assert lightonocr._select_runtime(torch_module) == expected


def test_grounding_parser_keeps_reading_order_and_only_recognized_text() -> None:
    raw = """![title](20,10,900,80)
# Annual **Report**

![image](10,90,400,500)
A generated description that is not OCR text.

![list+](30,510,950,650)
- Revenue
- Costs

![text](not-coordinates)
Malformed geometry still has *recognized text*.

![chart](30,700,950,980)
<table><tr><td>fabricated chart value</td></tr></table>
"""

    page = lightonocr.parse_grounding(raw)

    assert page == {
        "text": (
            "Annual Report\nRevenue\n\nCosts\n"
            "Malformed geometry still has recognized text."
        ),
        "blocks": [
            {
                "text": "Annual Report",
                "type": "title",
                "bbox": [[20, 10], [900, 10], [900, 80], [20, 80]],
            },
            {
                "text": "Revenue\n\nCosts",
                "type": "list+",
                "bbox": [[30, 510], [950, 510], [950, 650], [30, 650]],
            },
            {
                "text": "Malformed geometry still has recognized text.",
                "type": "text",
            },
        ],
    }


def test_grounding_parser_never_invents_invalid_geometry() -> None:
    page = lightonocr.parse_grounding(
        "![text](100,100,90,200)\nReverse\n"
        "![caption](-1,2,3,4)\nOutside\n"
        "![text](1,2,3)\nShort"
    )

    assert [block["text"] for block in page["blocks"]] == [
        "Reverse",
        "Outside",
        "Short",
    ]
    assert all("bbox" not in block for block in page["blocks"])


def test_grounding_parser_scales_to_original_non_square_image() -> None:
    page = lightonocr.parse_grounding(
        "![text](100,200,900,800)\nScaled", width=2000, height=500
    )

    assert page["blocks"][0]["bbox"] == [
        [200, 100],
        [1800, 100],
        [1800, 400],
        [200, 400],
    ]


def test_public_methods_use_grounding_for_ocr_and_image_only_for_markdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    events: list[object] = []
    processor = _Processor(
        calls,
        [
            "![text](100,200,900,800)\n**First** page",
            "# Plain page",
        ],
    )
    adapter = lightonocr.LightOnOCRAdapter(
        engine="lightonocr-3-0.8b",
        model=_Model(calls),
        processor=processor,
        torch_module=_fake_torch(),
        device="cpu",
    )

    class ImageModule:
        @staticmethod
        def open(stream: object) -> _Image:
            events.append(("open", stream.read()))
            return _Image("input", events, size=(4000, 2000))

    monkeypatch.setitem(sys.modules, "PIL", SimpleNamespace(Image=ImageModule))

    assert adapter.ocr([b"one"]) == [
        {
            "text": "First page",
            "blocks": [
                {
                    "text": "First page",
                    "type": "text",
                    "bbox": [
                        [400, 400],
                        [3600, 400],
                        [3600, 1600],
                        [400, 1600],
                    ],
                }
            ],
        }
    ]
    assert adapter.to_markdown("misleading.pdf", b"not a pdf") == {
        "markdown": "# Plain page",
        "ocr_used": [True],
    }

    conversations = calls["conversations"]
    assert conversations[0][0]["content"][1] == {
        "type": "text",
        "text": "grounding",
    }
    assert conversations[1][0]["content"] == [
        {"type": "image", "image": conversations[1][0]["content"][0]["image"]}
    ]
    assert all(
        options["enable_thinking"] is False for options in calls["template_options"]
    )
    assert all(
        options["max_new_tokens"] == 8192 for options in calls["generate_options"]
    )
    assert all(options["do_sample"] is False for options in calls["generate_options"])
    assert events.count(("thumbnail", "input", (2048, 2048))) == 2
    assert conversations[0][0]["content"][0]["image"].size == (2048, 1024)


def test_pdf_pages_render_and_generate_one_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    events: list[object] = []
    adapter = lightonocr.LightOnOCRAdapter(
        engine="lightonocr-3-4b",
        model=_Model(calls),
        processor=_Processor(calls, []),
        torch_module=_fake_torch(),
        device="cpu",
    )

    class Bitmap:
        def __init__(self, image: _Image) -> None:
            self.image = image

        def to_pil(self) -> _Image:
            events.append(("to-pil", self.image.name))
            return self.image

        def close(self) -> None:
            events.append(("bitmap-close", self.image.name))

    class Page:
        def __init__(self, index: int) -> None:
            self.index = index

        def render(self, *, scale: float) -> Bitmap:
            if self.index:
                assert ("image-close", "page-0") in events
            events.append(("render", self.index, scale))
            return Bitmap(_Image(f"page-{self.index}", events))

        def get_size(self) -> tuple[int, int]:
            return (1000, 2000)

        def close(self) -> None:
            events.append(("page-close", self.index))

    class PdfDocument:
        def __init__(self, data: bytes) -> None:
            events.append(("pdf-open", data))

        def __len__(self) -> int:
            return 2

        def __getitem__(self, index: int) -> Page:
            return Page(index)

        def close(self) -> None:
            events.append("pdf-close")

    monkeypatch.setitem(
        sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=PdfDocument)
    )

    def generate(image: _Image, prompt: str | None) -> str:
        events.append(("generate", image.name, prompt))
        return f"page {image.name[-1]}"

    monkeypatch.setattr(adapter, "_generate", generate)

    result = adapter.to_markdown("scan.jpg", b"\n %PDF-1.7 bytes")

    assert result == {"markdown": "page 0\n\npage 1", "ocr_used": [True, True]}
    assert [
        event for event in events if isinstance(event, tuple) and event[0] == "render"
    ] == [
        ("render", 0, pytest.approx(2048 / 2000)),
        ("render", 1, pytest.approx(2048 / 2000)),
    ]
    assert events[-1] == "pdf-close"


def test_generation_at_token_cap_is_reported_as_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    events: list[object] = []
    adapter = lightonocr.LightOnOCRAdapter(
        engine="lightonocr-3-0.8b",
        model=_Model(calls, generated_tokens=8192),
        processor=_Processor(calls, ["would be incomplete"]),
        torch_module=_fake_torch(),
        device="cpu",
    )

    class ImageModule:
        @staticmethod
        def open(stream: object) -> _Image:
            return _Image("large", events)

    monkeypatch.setitem(sys.modules, "PIL", SimpleNamespace(Image=ImageModule))

    with pytest.raises(RuntimeError, match="token limit"):
        adapter.to_markdown("page.png", b"png")


def test_real_image_and_pdf_preprocessing_with_stub_inference(monkeypatch):
    import io

    image_module = pytest.importorskip("PIL.Image")
    pytest.importorskip("pypdfium2")
    pypdf = pytest.importorskip("pypdf")
    adapter = lightonocr.LightOnOCRAdapter(
        engine="lightonocr-3-0.8b",
        model=None,
        processor=None,
        torch_module=None,
        device="cpu",
    )
    rendered = []

    def generate(image, prompt):
        rendered.append((image.size, image.getpixel((0, 0)), prompt))
        return "![text](0,0,1000,1000)\nHello" if prompt else "# Hello"

    monkeypatch.setattr(adapter, "_generate", generate)
    image = image_module.new("RGB", (3000, 1500), "white")
    png = io.BytesIO()
    image.save(png, format="PNG")
    image.close()
    page = adapter.ocr([png.getvalue()])[0]
    assert page["text"] == "Hello"
    assert page["blocks"][0]["bbox"] == [[0, 0], [3000, 0], [3000, 1500], [0, 1500]]
    assert rendered[-1] == ((2048, 1024), (255, 255, 255), "grounding")

    pdf = io.BytesIO()
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=792, height=612)
    writer.write(pdf)
    assert adapter.to_markdown("pages.pdf", pdf.getvalue()) == {
        "markdown": "# Hello\n\n# Hello",
        "ocr_used": [True, True],
    }
    assert len(rendered) == 3
    assert all(max(size) <= 2048 and prompt is None for size, _, prompt in rendered[1:])
