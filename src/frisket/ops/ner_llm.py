from __future__ import annotations


def entities_extract_instruction(
    labels: list[str], extra_instructions: str = ""
) -> str:
    base = (
        "Extract every named entity from the text of these types: "
        + ", ".join(labels)
        + ". For each entity return an object with `text` (the exact "
        "substring as it appears in the text), `type` (one of the "
        "requested types), `start` (the character offset in the provided "
        "text where the entity begins), and `end` (the character offset "
        "where it ends, exclusive). Only extract entities that are "
        "actually present in the text -- never invent one."
    )
    extra = (extra_instructions or "").strip()
    return f"{base}\n\n{extra}" if extra else base
