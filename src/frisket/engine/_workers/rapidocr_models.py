"""Dependency-light offline model discovery shared by runtime diagnostics.

RapidOCR itself is optional and imported only inside the resolver. Keeping the
exact det/cls/rec lookup here prevents the SDK runtime and health diagnostics
from drifting or accepting files split across roots that RapidOCR cannot load.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


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


__all__ = [
    "RapidOCRInvalidLanguage",
    "rapidocr_default_model_requirements",
    "rapidocr_model_requirements",
    "rapidocr_model_root_complete",
]
