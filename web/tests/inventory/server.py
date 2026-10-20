"""Start either real edition with bundled, stable pricing for visual fixtures."""

import sys
from unittest.mock import patch

from frisket.cli import main

if __name__ == "__main__":
    # This is screenshot fixture setup, not a production pricing behavior change.
    with patch("frisket.ai.llm.pricing_refresh.start_pricing_refresh"):
        if sys.argv[1:2] == ["--team"]:
            import uvicorn

            uvicorn.run(
                "frisket.team.asgi:app", host="127.0.0.1", port=int(sys.argv[2])
            )
        else:
            main()
