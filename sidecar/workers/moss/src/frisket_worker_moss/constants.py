"""Immutable MOSS-Transcribe-Diarize pins and prompt constants.

These are the model/server identity that flows into transcription receipts and
that the worker wrapper checks the adapter result against.  Bumping the model
requires a new revision here and a fresh live burn; nothing downstream may edit
these silently.
"""

from __future__ import annotations

from frisket_models.transcription.moss import (
    MOSS_ENGINE,
    MOSS_MODEL_ID,
    MOSS_MODEL_REVISION,
)

ENGINE = MOSS_ENGINE
MODEL_ID = MOSS_MODEL_ID
MODEL_REVISION = MOSS_MODEL_REVISION

# Image-committed serving identity: the device and dtype the built container's
# vLLM process actually runs on.  These are constants, not env knobs — a receipt
# must not be able to claim a dtype the server never used.  Changing them is a
# rebuild (a new OCI image digest), and the live burn asserts they are real.
SERVED_DEVICE = "cuda"
SERVED_DTYPE = "bfloat16"

# The OpenAI-compatible transcription route exposed by sgl-omni / vLLM.
TRANSCRIPTION_ROUTE = "/v1/audio/transcriptions"

# MOSS's default instruction already asks for start-timestamp + speaker-number
# segment openings and an end-timestamp close, i.e. the canonical
# ``[start][Sxx]text[end]`` grammar this leaf parses.  It is only sent
# explicitly when hotwords are appended; otherwise the server applies it by
# default and we omit ``prompt`` entirely.
DEFAULT_PROMPT = (
    "请将音频转写为文本，每一段需以起始时间戳和说话人编号"
    "（[S01]、[S02]、[S03]…）开头，正文为对应的语音内容，"
    "并在段末标注结束时间戳，以清晰标明该段语音范围。"
)

# Hotword hint appended to the default prompt when ``context`` is supplied.
HOTWORD_PREFIX = "热词提示："

# Decoder budget large enough to finish a long multi-speaker transcript
# (README raises this for ~90-minute inputs).  NOTE the field name: vLLM's
# transcription endpoint honours ``max_completion_tokens``; the sgl-omni docs'
# ``max_new_tokens`` is silently ignored by vLLM, so a wrong field name leaves
# the model on its ~5k default and truncates long audio.
DEFAULT_MAX_COMPLETION_TOKENS = 65536


def build_prompt(context: str | None) -> str | None:
    """Return the transcription prompt for an optional hotword ``context``.

    ``None`` context keeps the server on its default prompt (we send nothing).
    Any *supplied* context is forwarded — the frozen no-ignore rule means a knob
    the worker recorded as accepted must actually reach the model, so even a
    whitespace-only context re-sends the full default diarization instruction
    with the hint appended rather than being silently dropped.
    """

    if context is None:
        return None
    return f"{DEFAULT_PROMPT}{HOTWORD_PREFIX}{context}"
