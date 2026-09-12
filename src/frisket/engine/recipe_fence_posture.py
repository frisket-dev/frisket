"""What a `map.python` code recipe is confined by on THIS server, in one word.

The fence owns the answer: `engine/sandbox/fence.py` states the platform
matrix and its tests pin it. This module is the single place that turns that
matrix into a value a client can be told, and `GET /api/config` is the only
wire it goes out on. Nothing else recomputes it -- a browser cannot know the
server's platform, and two places computing "is this fenced?" is exactly how
the UI came to show a green "sandboxed" badge for a thing that was not.

The postures:

  ``enforced``  The fence installs a kernel fence in the recipe child before
                recipe code runs, or the run REFUSES. No recipe runs unfenced,
                so this can be reported without probing the kernel per run.
  ``partial``   Some OS-level wall, but not the whole perimeter. macOS today:
                a real `sandbox-exec` network wall and NO filesystem
                confinement.
  ``none``      Nothing below Python. Windows today.
  ``unknown``   A platform the fence has no stated answer for. Reported so the
                UI warns; it must never be read as "probably fine".

Derivation, not restatement: ``enforced`` is exactly "the fence hands this
platform a kernel policy" (`fence.bootstrap_policy`), and the unenforced
platforms are exactly the ones the fence names in its own notes. The only local fact is
which KIND of unenforced each is, and
`tests/server/test_runtime_config_recipe_fence.py` goes red if the fence's set
of unenforced platforms and this table's ever diverge.
"""

from __future__ import annotations

import sys
from typing import Literal

from frisket.engine.sandbox import fence

RecipeFencePosture = Literal["enforced", "partial", "none", "unknown"]

# Keyed by `sys.platform`, and required to have exactly the keys of the fence's
# own `_UNENFORCED_PLATFORM_NOTES` (closure test). The value says what survives
# on that platform, which is what the copy has to be able to distinguish:
# macOS keeps a network wall it can name, Windows has nothing to name.
_UNENFORCED_POSTURES: dict[str, RecipeFencePosture] = {
    "darwin": "partial",  # sandbox-exec network wall; no filesystem confinement
    "win32": "none",  # no seccomp, no Landlock, no seatbelt
}


def recipe_fence_posture() -> RecipeFencePosture:
    """This server's recipe-fence posture, for `GET /api/config`.

    Fails closed: a platform neither fenced nor named by the fence reports
    ``unknown``, which every consumer must treat as "warn".
    """
    policy = fence.bootstrap_policy(audit_netwall=True, recipe=True)
    if policy["fence"] is not None:
        return "enforced"
    return _UNENFORCED_POSTURES.get(sys.platform, "unknown")
