"""Named bootstrap for resolving pinned Parakeet artifacts.

Importing this module is standard-library-only.  The resolver's application
and optional Hub imports are deferred until the sandboxed child has started.
"""

from __future__ import annotations


def main() -> None:
    from frisket.engine._workers.parakeet_artifacts import resolver_main

    resolver_main()


if __name__ == "__main__":
    main()
