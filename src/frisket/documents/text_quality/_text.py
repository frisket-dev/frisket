"""Bounded text-only signals for obvious encoding damage and OCR gibberish."""

from __future__ import annotations

from collections import Counter
from functools import lru_cache
import gzip
import hashlib
from importlib.resources import files
import json
import math
import re
import statistics
import unicodedata


MAX_CHARS = 12_000

_MIN_EVIDENCE_CHARS = 24
_ENCODING_WARN_THRESHOLD = 0.56
_HAZARD = re.compile(
    "[\x00-\x08\x0e-\x1f\x7f-\x9f\ufffd\ue000-\uf8ff\U000f0000-\U0010ffff]"
)
_ZERO_WIDTH = re.compile("[\u200b\ufeff]")
_MOJIBAKE = re.compile(
    r"(?:\u00c3[\u0080-\u00bf\u00a0-\u00bf]|\u00c2[\u0080-\u00bf]|"
    r"\u00e2(?:[\u0080-\u009f]|[\u20ac\u2122\u0152\u0153\u201a])|"
    r"\u00f0[\u009f\u0178]|\u00ef\u00bf\u00bd)"
)
_CID = re.compile(r"(?:\(cid:\s*\d+\)|\bcid:\s*\d+\b)", re.IGNORECASE)
_TOKEN = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)
_ENCODING_TOKEN = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)
_REPEATED_WORD = re.compile(r"\b([^\W_]{2,})\b(?:\s+\1\b){2,}", re.IGNORECASE)
_REPEATED_PATTERN = re.compile(r"([^\s._-]{2,12})\1{3,}")

_MIN_LATIN_TOKENS = 20
_BITS = 1_500_007
_HASHES = 8
_LATIN_FALLBACK = str.maketrans(
    {"ł": "l", "đ": "d", "ð": "d", "þ": "th", "æ": "ae", "œ": "oe"}
)


def has_encoding_damage(text: str) -> bool:
    """Detect obvious damaged encodings and highly corrupted surface text."""

    sample = text[:MAX_CHARS]
    visible = [character for character in sample if not character.isspace()]
    count = len(visible)
    if not count:
        return False

    hazards = len(_HAZARD.findall(sample))
    mojibake = len(_MOJIBAKE.findall(sample))
    cid = len(_CID.findall(sample))
    zero_width = len(_ZERO_WIDTH.findall(sample))
    tokens = _ENCODING_TOKEN.findall(sample)
    alpha_tokens = buried_digits = chaotic_case = repeated_letter = 0
    for token in tokens:
        core = token.replace("-", "").replace("'", "").replace("’", "")
        letters = [character for character in core if character.isalpha()]
        if not letters:
            continue
        alpha_tokens += 1
        if (
            any(character.isdigit() for character in core)
            and any(character.islower() for character in letters)
            and all(character.isascii() for character in letters)
        ):
            classes = [
                "a" if character.isalpha() else "d" if character.isdigit() else "x"
                for character in core
            ]
            transitions = sum(
                left != right for left, right in zip(classes, classes[1:])
            )
            embedded = any(
                core[index].isdigit()
                and core[index - 1].isalpha()
                and core[index + 1].isalpha()
                for index in range(1, len(core) - 1)
            )
            buried_digits += embedded or transitions >= 3
        case_changes = sum(
            (left.islower() and right.isupper()) or (left.isupper() and right.islower())
            for left, right in zip(letters, letters[1:])
        )
        chaotic_case += (
            len(letters) >= 6
            and case_changes >= 4
            and sum(character.islower() for character in letters) >= 2
            and sum(character.isupper() for character in letters) >= 2
        )
        repeated_letter += (
            len(letters) >= 3
            and len({character.casefold() for character in letters}) == 1
        )

    no_space_lines = sum(
        sum(character.isascii() and character.isalpha() for character in line) >= 80
        and sum(character.isascii() and character.isalpha() for character in line)
        / max(1, len(line))
        >= 0.75
        and line.count(" ") <= 1
        for line in (sample.splitlines() or [sample])
    )
    repeated_runs = len(_REPEATED_WORD.findall(sample)) + len(
        _REPEATED_PATTERN.findall(sample)
    )
    line_counts = Counter(
        line.strip() for line in sample.splitlines() if len(line.strip()) >= 12
    )
    repeated_lines = sum(value - 2 for value in line_counts.values() if value >= 3)
    symbol_count = sum(not character.isalnum() for character in visible)

    scores: list[float] = []
    if hazards:
        scores.append(min(0.86, 0.18 + 19 * hazards / count))
    if mojibake:
        scores.append(min(0.78, 0.30 + 0.12 * mojibake))
    if cid:
        scores.append(min(0.88, 0.40 + 0.08 * cid))
    if zero_width >= 3:
        scores.append(min(0.62, 0.38 + 0.04 * zero_width))
    mixed_rate = buried_digits / max(1, alpha_tokens)
    if mixed_rate >= 0.07:
        scores.append(min(0.60, 0.10 + 1.4 * mixed_rate))
    case_rate = chaotic_case / max(1, alpha_tokens)
    if case_rate >= 0.08:
        scores.append(min(0.60, 0.10 + 2.0 * case_rate))
    if repeated_letter:
        scores.append(min(0.40, 0.25 + 0.05 * repeated_letter))
    if no_space_lines:
        scores.append(min(0.62, 0.50 + 0.08 * no_space_lines))
    if repeated_runs or repeated_lines:
        scores.append(min(0.45, 0.24 + 0.06 * (repeated_runs + repeated_lines)))
    if (
        count >= 20
        and symbol_count / count >= 0.60
        and (count - symbol_count) / count <= 0.25
    ):
        scores.append(0.68)

    strong_short = bool(hazards or mojibake or cid)
    return (count >= _MIN_EVIDENCE_CHARS or strong_short) and sum(
        scores
    ) >= _ENCODING_WARN_THRESHOLD


def has_garbled_text(text: str) -> bool:
    """Detect source-sized text resembling observed Tesseract gibberish."""

    sample = text[:MAX_CHARS]
    raw_tokens = _TOKEN.findall(sample)
    eligible = [
        folded for token in raw_tokens if (folded := _model_token(token)) is not None
    ]
    token_count = len(eligible)

    letters = [character for character in sample if character.isalpha()]
    cjk_share = sum(_is_cjk(character) for character in letters) / max(1, len(letters))
    upper_runs = len(re.findall(r"\b[A-Z]{7,}\b", sample))
    if token_count >= 8 and cjk_share >= 0.20:
        upper_rate = upper_runs / token_count
        if upper_rate >= 0.325:
            return True

    if token_count < _MIN_LATIN_TOKENS:
        return False

    per_line = [
        sum(_latin_fold(token) is not None for token in _TOKEN.findall(line))
        for line in sample.splitlines()
        if line.strip()
    ]
    line_tokens = sum(per_line)
    long_line_share = sum(value for value in per_line if value >= 6) / max(
        1, line_tokens
    )
    if long_line_share < 0.40:
        return False

    counts, total, bloom = _assets()
    unknown = sum(not _known(token, bloom) for token in eligible)
    unknown_rate = unknown / token_count
    nlls: list[float] = []
    for token in eligible:
        padded = f"^{token}$"
        grams = [padded[index : index + 3] for index in range(len(padded) - 2)]
        nlls.append(
            statistics.fmean(
                -math.log(
                    (int(counts.get(gram, 0)) + 0.25) / (total + 0.25 * len(counts))
                )
                for gram in grams
            )
        )
    trigram_nll = statistics.fmean(nlls)
    folded_all = [
        folded for token in raw_tokens if (folded := _latin_fold(token)) is not None
    ]
    fragments = sum(len(token) == 1 and token not in {"a", "i"} for token in folded_all)
    fragment_rate = fragments / max(1, len(folded_all))

    lexical = _clip((unknown_rate - 0.60) / 0.30)
    ngram = _clip((trigram_nll - 8.0) / 1.0)
    fragments = _clip((fragment_rate - 0.05) / 0.09)
    return min(lexical, ngram, fragments) >= 0.50


@lru_cache(maxsize=1)
def _assets() -> tuple[dict[str, int], int, bytes]:
    package = files(__package__)
    with package.joinpath("latin_trigrams.json.gz").open("rb") as raw:
        with gzip.open(raw, "rt", encoding="utf-8") as stream:
            model = json.load(stream)
    bloom = package.joinpath("english_lexicon.bloom").read_bytes()
    return model["counts"], int(model["trigram_total"]), bloom


def _positions(word: str):
    digest = hashlib.blake2b(
        word.encode(), digest_size=16, person=b"frisket-ocr-v1"
    ).digest()
    first = int.from_bytes(digest[:8], "big")
    step = int.from_bytes(digest[8:], "big") | 1
    for index in range(_HASHES):
        yield (first + index * step) % _BITS


def _known(word: str, bloom: bytes) -> bool:
    return all(
        bloom[position // 8] & (1 << (position % 8)) for position in _positions(word)
    )


def _clip(value: float) -> float:
    return max(0.0, min(1.0, value))


@lru_cache(maxsize=1024)
def _is_latin(character: str) -> bool:
    return "LATIN" in unicodedata.name(character, "")


def _latin_fold(token: str) -> str | None:
    letters = [character for character in token if character.isalpha()]
    if not letters or any(not _is_latin(character) for character in letters):
        return None
    folded = "".join(
        character
        for character in unicodedata.normalize("NFD", token.casefold())
        if not unicodedata.combining(character)
    ).translate(_LATIN_FALLBACK)
    folded = folded.replace("’", "'")
    return "".join(
        character
        for character in folded
        if character.isascii() and (character.isalpha() or character in "'-")
    )


def _model_token(token: str) -> str | None:
    folded = _latin_fold(token)
    if not folded:
        return None
    plain = folded.replace("-", "").replace("'", "")
    if not 3 <= len(plain) <= 24:
        return None
    original_letters = "".join(character for character in token if character.isalpha())
    if (original_letters.isupper() and len(original_letters) <= 6) or re.fullmatch(
        r"[ivxlcdm]+", plain
    ):
        return None
    return plain


def _is_cjk(character: str) -> bool:
    code = ord(character)
    return (
        0x3040 <= code <= 0x30FF
        or 0x3400 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x3134F
    )
