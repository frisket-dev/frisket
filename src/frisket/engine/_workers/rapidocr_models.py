"""Dependency-light offline model discovery shared by runtime diagnostics.

RapidOCR itself is optional and imported only inside the resolver. Keeping the
exact det/cls/rec lookup here prevents the SDK runtime and health diagnostics
from drifting or accepting files split across roots that RapidOCR cannot load.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


# RapidOCR 3.8.1 bundles these verified ONNX files, while its own resolver can
# still request the older ``*_mobile.onnx`` aliases.  The installed package is
# readonly; callers may copy this known trio into Frisket's shared cache.
_BUNDLED_3_8_1_MODEL_HASHES = {
    "ch_PP-OCRv4_det_infer.onnx": (
        "d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9"
    ),
    "ch_ppocr_mobile_v2.0_cls_infer.onnx": (
        "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c"
    ),
    "ch_PP-OCRv4_rec_infer.onnx": (
        "48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b"
    ),
}


class RapidOCRInvalidLanguage(ValueError):
    """A language is not one of RapidOCR's recognition model families."""


@lru_cache(maxsize=32)
def rapidocr_model_requirements(
    language: str | None = None,
) -> tuple[Path, tuple[str, ...]] | None:
    """Resolve exact det/cls/requested-rec filenames for one language.

    Invalid languages are distinguished from an unavailable/broken optional
    dependency so the parent can defer their user-facing validation to the
    worker's ordinary per-row error path.
    """

    try:
        from rapidocr.inference_engine.base import FileInfo, InferSession
        from rapidocr.main import DEFAULT_CFG_PATH, root_dir
        from rapidocr.utils.parse_parameters import ParseParams
        from rapidocr.utils.typings import LangRec

        cfg = ParseParams.load(DEFAULT_CFG_PATH)
    except Exception:
        return None

    if language is None:
        recognition_language = cfg["Rec"].lang_type
    else:
        try:
            recognition_language = LangRec(language)
        except (TypeError, ValueError) as error:
            raise RapidOCRInvalidLanguage(language) from error

    try:
        filenames: list[str] = []
        for section in ("Det", "Cls", "Rec"):
            section_cfg = cfg[section]
            model_dict = InferSession.get_model_url(
                FileInfo(
                    engine_type=section_cfg.engine_type,
                    ocr_version=section_cfg.ocr_version,
                    task_type=section_cfg.task_type,
                    lang_type=(
                        recognition_language
                        if section == "Rec"
                        else section_cfg.lang_type
                    ),
                    model_type=section_cfg.model_type,
                )
            )
            filename = Path(str(model_dict["model_dir"])).name
            if not filename:
                return None
            filenames.append(filename)
    except Exception:
        return None
    if len(set(filenames)) != 3:
        return None
    return root_dir / "models", tuple(filenames)


@lru_cache(maxsize=1)
def rapidocr_default_model_requirements() -> tuple[Path, tuple[str, ...]] | None:
    """Resolve the default-language model requirements for diagnostics."""

    return rapidocr_model_requirements()


def rapidocr_model_root_complete(root: Path, filenames: tuple[str, ...]) -> bool:
    """Whether one model root contains every required regular file."""

    try:
        return all((root / filename).is_file() for filename in filenames)
    except OSError:
        return False


def rapidocr_bundled_model_aliases(
    root: Path, filenames: tuple[str, ...]
) -> tuple[tuple[Path, str, str], ...] | None:
    """Return the known 3.8.1 bundled sources for requested legacy names.

    This is discovery only. The runtime copies a hash-verified result into its
    writable shared cache, never into the installed RapidOCR package.
    """

    sources = {}
    for name, digest in _BUNDLED_3_8_1_MODEL_HASHES.items():
        sources[name] = (name, digest)
        sources[name.replace("_infer.onnx", "_mobile.onnx")] = (name, digest)
    aliases: list[tuple[Path, str, str]] = []
    for filename in filenames:
        source = sources.get(filename)
        if source is None:
            return None
        source_name, digest = source
        aliases.append((root / source_name, filename, digest))
    return tuple(aliases)


__all__ = [
    "RapidOCRInvalidLanguage",
    "rapidocr_default_model_requirements",
    "rapidocr_bundled_model_aliases",
    "rapidocr_model_requirements",
    "rapidocr_model_root_complete",
]
