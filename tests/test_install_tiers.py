"""Packaging and runtime contracts for the three blessed install tiers."""

from __future__ import annotations

import importlib
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STANDARD_COMPONENTS = ("ner-spacy", "asr", "translate", "pdf", "browser")
COMPLETE_ADDITIONS = ("cloud", "models", "translate-gguf", "entities")


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]


def _requirement_names(requirements: list[str]) -> set[str]:
    return {
        re.split(r"[\s\[<>=!~;]", requirement.strip(), maxsplit=1)[0].casefold()
        for requirement in requirements
    }


def test_base_owns_semantic_ocr_and_conversion_runtimes() -> None:
    project = _project()
    base_names = _requirement_names(project["dependencies"])

    assert {
        "fastembed",
        "rapidocr",
        "onnxruntime",
        "pytesseract",
        "markitdown",
    } <= base_names
    assert not (
        {"semantic", "ocr", "convert"} & project["optional-dependencies"].keys()
    )


def test_base_conversion_metadata_and_runtime_preserve_html_support(
    tmp_path: Path,
) -> None:
    """HTML is MarkItDown core; ``html`` is not a declared package extra."""

    requirements = _project()["dependencies"]
    markitdown = next(req for req in requirements if req.startswith("markitdown"))
    assert markitdown == "markitdown[docx,pdf]>=0.1.6"

    source = tmp_path / "story.html"
    source.write_text(
        "<html><body><h1>Heading</h1><p>Hello world</p></body></html>",
        encoding="utf-8",
    )
    converted = importlib.import_module("markitdown").MarkItDown().convert(source)
    assert "# Heading" in converted.text_content
    assert "Hello world" in converted.text_content


def test_standard_is_the_exact_wheel_friendly_component_union() -> None:
    extras = _project()["optional-dependencies"]
    expected = {
        requirement
        for component in STANDARD_COMPONENTS
        for requirement in extras[component]
    }

    assert set(extras["standard"]) == expected
    assert "followthemoney" not in _requirement_names(extras["standard"])


def test_complete_is_standard_plus_the_platform_fragile_components() -> None:
    extras = _project()["optional-dependencies"]
    expected = {
        *extras["standard"],
        *(
            requirement
            for component in COMPLETE_ADDITIONS
            for requirement in extras[component]
        ),
    }

    assert set(extras["complete"]) == expected
    assert {"docling", "llama-cpp-python", "followthemoney"} <= _requirement_names(
        extras["complete"]
    )


def test_base_runtime_modules_import_in_the_default_test_environment() -> None:
    """The default sync must provide executable runtimes, not metadata only."""

    for module in ("fastembed", "rapidocr", "onnxruntime", "pytesseract", "markitdown"):
        importlib.import_module(module)
