"""Private local Tesseract OCR worker.

One-shot, per-row: launched as ``python -m frisket.engine._workers.tesseract_worker``
inside the sandboxed subprocess ``ops/ocr.py``'s ``_ocr_tesseract`` spawns via
``run_sandboxed`` (sandbox/shim.py), driving the system ``tesseract`` binary
through pytesseract's ``image_to_data`` (word-level boxes + confidence).
``PATH`` is passed through so this worker can find the brew/apt binary.
Request JSON arrives on stdin; the response JSON is written to the file named
by ``payload["out"]``, matching the rapidocr worker's result-file convention.
This module is deliberately standard-library-only at import time;
``pytesseract`` is imported only inside ``main`` once the sandbox owns the
process.
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    payload = json.load(sys.stdin)
    try:
        import pytesseract
        from pytesseract import Output
    except ImportError as e:
        _write(
            payload,
            {
                "error": "tesseract wrapper not importable in the "
                "worker env (%s); repair the base Frisket install" % e
            },
        )
        return
    lang = payload.get("language") or None
    pages = []
    for path in payload["paths"]:
        # Pass the on-disk path straight to tesseract (like the rapidocr
        # worker), NOT a re-opened PIL image: pytesseract re-saves a PIL image
        # to a temp PNG that the tesseract child cannot read inside the
        # sandbox, so the direct path is both correct and cheaper.
        data = pytesseract.image_to_data(path, lang=lang, output_type=Output.DICT)
        blocks = []
        n = len(data.get("text", []))
        for i in range(n):
            txt = (data["text"][i] or "").strip()
            if not txt:
                continue
            x, y = int(data["left"][i]), int(data["top"][i])
            w, h = int(data["width"][i]), int(data["height"][i])
            block = {
                "text": txt,
                "bbox": [[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
            }
            try:
                conf = float(data["conf"][i])
                if conf >= 0:  # -1 marks non-text/blank groupings
                    block["score"] = round(conf / 100.0, 4)
            except (KeyError, TypeError, ValueError):
                pass
            blocks.append(block)
        pages.append(
            {"text": " ".join(b["text"] for b in blocks).strip(), "blocks": blocks}
        )
    _write(payload, {"pages": pages})


def _write(payload, obj):
    with open(payload["out"], "w") as f:
        json.dump(obj, f)


if __name__ == "__main__":
    main()
