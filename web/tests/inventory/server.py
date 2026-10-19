"""Start the real local server with bundled, stable pricing for visual fixtures."""

from unittest.mock import patch

from frisket.cli import main

if __name__ == "__main__":
    # This is screenshot fixture setup, not a production pricing behavior change.
    with patch("frisket.ai.llm.pricing_refresh.start_pricing_refresh"):
        main()
