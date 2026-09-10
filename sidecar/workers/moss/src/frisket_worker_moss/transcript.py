"""Parser for MOSS-Transcribe-Diarize's compact transcript grammar.

The model emits concatenated segments::

    [start][Sxx]text[end][start][Sxx]text[end]...

The text may itself contain bracketed tokens, including numeric tokens such as
``[2024]`` and acoustic events such as ``[laughter]``.  Consequently an end
timestamp is the *last* numeric bracket before the next complete segment opener
(or the end of the response), not the first numeric bracket after the speaker.

Detectably malformed model output fails closed.  In particular, the parser
does not sort out-of-order segments or return a partial transcript after a
truncated tail: both would turn an engine fault into an apparently complete
success.  This module stays import-light so its recorded-output gate runs on
CPU.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


# The reference parser accepts integer timestamps and decimal timestamps with
# a leading or trailing digit omitted (``.5`` / ``1.``), so retain that useful
# tolerance while excluding signs, exponent notation, NaN, and infinity.
_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)"
_SEGMENT_OPEN = re.compile(rf"\[({_NUMBER})\]\[(S\d+)\]")
_TIMESTAMP = re.compile(rf"\[({_NUMBER})\]")


@dataclass(frozen=True, slots=True)
class ParsedSegment:
    start: float
    end: float
    speaker: str
    text: str


class MossTranscriptError(ValueError):
    """The native server returned output outside MOSS's canonical grammar."""


def _finite_timestamp(token: str, *, label: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise MossTranscriptError(f"{label} timestamp is not finite")
    return value


def _parse_body(
    body: str,
    *,
    start: float,
    speaker: str,
    segment_index: int,
) -> ParsedSegment | None:
    """Parse one opener-delimited body using its final timestamp as the close."""

    timestamps = list(_TIMESTAMP.finditer(body))
    if not timestamps:
        raise MossTranscriptError(
            f"segment {segment_index} ({speaker} at {start:g}s) has no end timestamp"
        )

    end_token = timestamps[-1]
    trailing = body[end_token.end() :]
    if trailing.strip():
        raise MossTranscriptError(
            f"segment {segment_index} ({speaker} at {start:g}s) has "
            "unparseable content after its end timestamp"
        )

    if len(timestamps) > 1:
        prior_token = timestamps[-2]
        between = body[prior_token.end() : end_token.start()]
        if not between.strip():
            # ``...[end][next_start]`` cut before the next speaker tag is byte-
            # identical to numeric bracketed text immediately followed by an
            # end.  Reject the ambiguity instead of fabricating a later close.
            raise MossTranscriptError(
                f"segment {segment_index} ({speaker} at {start:g}s) ends with "
                "ambiguous adjacent timestamp tokens"
            )

    end = _finite_timestamp(end_token.group(1), label=f"segment {segment_index} end")
    if end < start:
        raise MossTranscriptError(
            f"segment {segment_index} ({speaker}) ends at {end:g}s before "
            f"its {start:g}s start"
        )

    content = body[: end_token.start()].strip()
    if not content:
        # Match the model authors' skip-empty behavior.  A structurally complete
        # empty response is a legitimate representation of silence.
        return None
    return ParsedSegment(start=start, end=end, speaker=speaker, text=content)


def parse_moss_transcript(text: str) -> list[ParsedSegment]:
    """Parse a raw MOSS transcript into speaker segments in emitted order.

    Empty/whitespace-only output is accepted as silence.  Every non-empty
    response must be canonical; detectable truncation, junk, backwards spans,
    ambiguous adjacent timestamps, and decreasing starts raise
    :class:`MossTranscriptError` so the worker returns an inference failure
    rather than an incomplete or reordered result.
    """

    raw = text.strip()
    if not raw:
        return []

    opens = list(_SEGMENT_OPEN.finditer(raw))
    if not opens:
        raise MossTranscriptError(
            "MOSS output contained no [start][Sxx]text[end] segment"
        )
    if opens[0].start() != 0:
        raise MossTranscriptError("MOSS output has content before its first segment")

    segments: list[ParsedSegment] = []
    previous_start: float | None = None
    for index, opener in enumerate(opens):
        start = _finite_timestamp(opener.group(1), label=f"segment {index} start")
        speaker = opener.group(2)
        if previous_start is not None and start < previous_start:
            raise MossTranscriptError(
                f"segment {index} starts at {start:g}s before the prior "
                f"{previous_start:g}s start"
            )
        previous_start = start

        body_end = opens[index + 1].start() if index + 1 < len(opens) else len(raw)
        body = raw[opener.end() : body_end]
        parsed = _parse_body(
            body,
            start=start,
            speaker=speaker,
            segment_index=index,
        )
        if parsed is not None:
            segments.append(parsed)

    return segments
