"""``python -m frisket.cli`` entry point.

The serve path respawns the job worker as ``python -m frisket.cli worker``
(``_worker_argv`` / ``_database_worker_argv``), and release preflight asserts
``frisket = "frisket.cli:main"``; both resolve through the same ``main`` the
console script uses.
"""

from frisket.cli import main

if __name__ == "__main__":
    main()
