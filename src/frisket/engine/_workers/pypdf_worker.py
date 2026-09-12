"""One-shot PDF metadata worker.

The parser imports only after the runtime's isolated bootstrap has started.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def main() -> None:
    try:
        import pypdf
        from pypdf import PdfReader

        reader = PdfReader(sys.argv[1], strict=False)
        out: dict[str, Any] = {
            "version": getattr(pypdf, "__version__", None),
            "encrypted": bool(reader.is_encrypted),
            "metadata": {},
            "pages": [],
            "page_count": None,
        }
        try:
            out["page_count"] = len(reader.pages)
            for index, page in enumerate(reader.pages[:128]):
                box = page.mediabox
                out["pages"].append(
                    {
                        "index": index,
                        "width_points": round(float(box.width), 6),
                        "height_points": round(float(box.height), 6),
                        "rotation_degrees": int(page.get("/Rotate", 0) or 0),
                    }
                )
        except Exception:
            pass
        try:
            metadata = reader.metadata or {}
            for key in (
                "/Title",
                "/Author",
                "/Subject",
                "/Creator",
                "/Producer",
                "/CreationDate",
                "/ModDate",
                "/Keywords",
            ):
                value = metadata.get(key)
                if value is not None:
                    out["metadata"][key] = str(value)
        except Exception:
            pass
        print(json.dumps(out, sort_keys=True, separators=(",", ":"), allow_nan=False))
    except ModuleNotFoundError:
        print(json.dumps({"dependency_missing": True}, separators=(",", ":")))
    except Exception:
        print(json.dumps({"parse_error": True}, separators=(",", ":")))


if __name__ == "__main__":
    main()
