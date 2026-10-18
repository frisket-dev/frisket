from __future__ import annotations

import cv2
import numpy as np
import pytest

from frisket.actions.document_extraction_types import Box, PositionedToken
from frisket.documents.text_quality import TextQualityFlag, assess_text_quality


_CLEAN_LEDGER_OCR = """SUMMIT RESPONSE ACADEMY
Field equipment issue ledger
Batch SR-907
Exercise ORBIT * Warehouse C * 18 October 2026
Assignee Unit / serial Issued Return Condition
BX-204 Elara Finch VHF / VR-7812 07:40 16:15 Checked
BX-219 Milo Ardent GPS / GP-4407 07:42 16:08 Checked
CX-031 Zara Nwosu AED / AD-1935 07:44 16:22 Seal OK
CX-048 Ruben Sato VHF / VR-7849 07:45 16:26 Checked
DX-116 Talia Greer LAMP / LM-6021 07:48 16:29 Charged
DX-124 Kian Basu GPS / GP-4460 07:51 16:11 Checked
All people, badge codes, and equipment serials are invented for OCR evaluation.
"""

_CLEAN_CJK_OCR = """秋季防災訓練 プログラム 訓練番号 TK-268
地域の避難所運営を想定し、受付、情報伝達、応急手当、物資配布の手順を確認します。
08:40 受付開始・名札配布 受付班 体育館入口 REG-61
09:00 開会説明と安全確認 本部班 体育館 SAFE-3
10:00 無線連絡訓練 情報班 会議室 VHF-12
11:15 飲料水と毛布の配布 物資班 倉庫前 SUP-69
13:00 多言語案内の練習 情報班 会議室B LANG-2
緊急時は本部班の指示を優先し、個人情報を含む用紙は終了後に回収してください。
"""

_ROTATED_OCR = """T/T psoda1 Buljeaw |euolqoy Yeip paroiddy
00:9T 1940390 ST Aq Go] UoIqedYLIe BY} a3e ND119 ajeA UEMO
48Q0}90 ZZ UO BUBAUOIAL ||IM
jaued uoenjers ay} pue 19G0}90 OZ UO UOOU je aNp aie sasuodsay
4aq0}90 ST UO 0O:9T Aq suo!jsanb JeDQuUAap! anss! ||IM JUaWAINIOIg
BUO 0} SA}0A JNOy Aq puNos UO!edYIVe WoYs e padoidde jiounod ayy
suol}0e pue UOISsIDEg Pp puaxaem JaAopueY ay} 40} Jaau|Bua Aynp
poaweu e pue }saj} Ye}Sa1 INOY 3YyHIa AYO e BpN|du! pjnoys ajnpayds
"""


def _token(text: str, x0: float, y0: float, x1: float, y1: float) -> PositionedToken:
    return PositionedToken(text=text, box=Box(x0=x0, y0=y0, x1=x1, y1=y1))


def test_empty_text_is_a_specific_flag() -> None:
    assert assess_text_quality(" \n\t") == (TextQualityFlag.EMPTY,)


@pytest.mark.parametrize("text", [_CLEAN_LEDGER_OCR, _CLEAN_CJK_OCR])
def test_representative_names_numbers_and_cjk_are_not_flagged(text: str) -> None:
    assert assess_text_quality(text) == ()


def test_real_rotated_ocr_excerpt_is_flagged_as_garbled() -> None:
    assert assess_text_quality(_ROTATED_OCR) == (TextQualityFlag.GARBLED_TEXT,)


def test_obvious_mojibake_is_kept_separate_from_ocr_gibberish() -> None:
    assert assess_text_quality("Meeting notes: cafÃ© cafÃ© cafÃ©") == (
        TextQualityFlag.ENCODING_DAMAGE,
    )


def test_text_checks_stop_at_twelve_thousand_characters() -> None:
    prefix = ("The council reviewed the ordinary meeting minutes. " * 300)[:12_000]
    assert len(prefix) == 12_000
    assert assess_text_quality(prefix + " cafÃ© cafÃ© cafÃ©") == assess_text_quality(
        prefix
    )


def test_existing_positioned_tokens_detect_predominantly_vertical_words() -> None:
    tokens = [
        _token(f"word{index}", 0.02 * index, 0.1, 0.02 * index + 0.01, 0.5)
        for index in range(24)
    ]
    assert assess_text_quality("Recognized text", positioned_tokens=tokens) == (
        TextQualityFlag.PREDOMINANTLY_VERTICAL_WORDS,
    )


def test_unboxed_textlike_ink_is_flagged_but_explained_ink_is_not() -> None:
    image = np.full((400, 600), 255, dtype=np.uint8)
    for index, line in enumerate(
        (
            "Vendor review minutes",
            "Three compliant proposals",
            "Maintenance access schedule",
            "Responses are due Friday",
        )
    ):
        cv2.putText(
            image,
            line,
            (40, 70 + index * 70),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            0,
            2,
            cv2.LINE_AA,
        )

    assert assess_text_quality("Partial extraction", grayscale_thumbnail=image) == (
        TextQualityFlag.UNEXPLAINED_TEXTLIKE_INK,
    )

    covering_tokens = [
        _token("line", 0.04, 0.10, 0.95, 0.20),
        _token("line", 0.04, 0.25, 0.95, 0.37),
        _token("line", 0.04, 0.42, 0.95, 0.55),
        _token("line", 0.04, 0.60, 0.95, 0.72),
    ]
    assert (
        assess_text_quality(
            "Complete extraction",
            grayscale_thumbnail=image,
            positioned_tokens=covering_tokens,
        )
        == ()
    )


def test_diagram_shapes_do_not_look_like_unexplained_text() -> None:
    image = np.full((400, 600), 255, dtype=np.uint8)
    tokens = []
    for x, label in ((45, "INPUT"), (235, "PROCESS"), (445, "OUTPUT")):
        cv2.rectangle(image, (x - 15, 100), (x + 115, 190), 0, 4)
        (width, height), _baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1
        )
        cv2.putText(
            image,
            label,
            (x, 150),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            0,
            1,
            cv2.LINE_AA,
        )
        tokens.append(
            _token(
                label,
                x / 600,
                (150 - height) / 400,
                (x + width) / 600,
                150 / 400,
            )
        )
    cv2.arrowedLine(image, (160, 145), (220, 145), 0, 4)
    cv2.arrowedLine(image, (350, 145), (430, 145), 0, 4)

    assert (
        assess_text_quality(
            "INPUT PROCESS OUTPUT",
            grayscale_thumbnail=image,
            positioned_tokens=tokens,
        )
        == ()
    )


def test_page_image_must_be_grayscale_uint8() -> None:
    with pytest.raises(ValueError, match="non-empty 2D"):
        assess_text_quality(
            "text", grayscale_thumbnail=np.zeros((4, 4, 3), dtype=np.uint8)
        )
    with pytest.raises(ValueError, match="uint8"):
        assess_text_quality(
            "text", grayscale_thumbnail=np.zeros((4, 4), dtype=np.float32)
        )
