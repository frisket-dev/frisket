"""Dependency-light inference shared by host installs and the offline worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def translate_directory(
    directory: Path, texts: list[str], translator: Any
) -> list[str]:
    import sentencepiece

    sp_src = sentencepiece.SentencePieceProcessor(
        model_file=str(directory / "source.spm")
    )
    sp_tgt = sentencepiece.SentencePieceProcessor(
        model_file=str(directory / "target.spm")
    )

    out: list[str] = []
    # Opus-MT / Marian models REQUIRE the source terminated with the EOS token
    # `</s>`: the SentencePiece pieces alone leave the decoder with no sentence
    # boundary and it degenerates into a repetition loop ("Hola hola hola...").
    # Appending `</s>` (as the MarianTokenizer does) is what makes the CT2
    # weights produce faithful output ("Hello, world." -> "Hola, mundo."). A
    # small beam gives a stable hypothesis over greedy decoding.
    batch_tokens = [sp_src.encode(text, out_type=str) + ["</s>"] for text in texts]
    results = translator.translate_batch(batch_tokens, beam_size=4)
    for result in results:
        hypothesis = result.hypotheses[0] if result.hypotheses else []
        # Drop any trailing EOS the model emits before detokenizing.
        if hypothesis and hypothesis[-1] == "</s>":
            hypothesis = hypothesis[:-1]
        out.append(sp_tgt.decode(hypothesis))
    return out
