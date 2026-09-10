import pytest

from frisket.ops.ocr_engines import _tesseract_language_code


def test_tesseract_uses_one_user_facing_language_vocabulary() -> None:
    assert _tesseract_language_code(None) is None
    assert _tesseract_language_code("en") == "eng"
    assert _tesseract_language_code(" EN ") == "eng"

    with pytest.raises(ValueError, match="available: en"):
        _tesseract_language_code("japan")


def test_ocr_catalog_names_tesseracts_supported_language() -> None:
    from frisket.server.action_catalog_hints import _recipe_engines

    engine = next(
        item for item in _recipe_engines("media.ocr", {}) if item["id"] == "tesseract"
    )
    assert engine["language"]["fixed_language"] == "en"
    assert engine["language"]["choices"] == [{"value": "en", "label": "English"}]
