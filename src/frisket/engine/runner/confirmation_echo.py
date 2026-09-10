"""The one confirmed-retry echo comparison behind every 402 gate.

Every ``needs_confirmation`` gate mints a quote/claims hash
(``promise_set_hash`` on the envelope) and requires the retry to echo it
byte-for-byte via ``consented_promise_set_hash`` alongside
``confirmed=true``. ``confirmed`` alone is only the retry-flow flag: a bare
boolean with no echo (or a stale echo) is not consent — the caller would be
approving whatever quote happens to exist at retry time, including one it
was never shown.

This comparison used to be hand-written per executor family; a family that
declared the ``confirmed`` + ``consented_promise_set_hash`` fields but
forgot the comparison stayed green under the declaration-side closure test
(``tests/engine/test_cost_policy_confirmation_invariant.py``). Families now
call :func:`refuse_unless_exact_echo`; the behavioral closure test
(``tests/engine/test_confirmation_echo_gate_closure.py``) dispatches every
confirmation-requiring kind with ``confirmed=true`` and a wrong hash and
asserts it refuses without executing.

Deliberately dependency-free (stdlib only): it is imported by the runner
validation gates, the executor families, and the authoring workbench without
any layering or import-cycle concerns.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

_RefusalT = TypeVar("_RefusalT")


def refuse_unless_exact_echo(
    *,
    confirmed: bool,
    echoed_hash: object,
    expected_hash: str | None,
    refuse: Callable[[], _RefusalT],
) -> _RefusalT | None:
    """Return ``None`` only for ``confirmed`` plus a byte-exact echo.

    Anything else — unconfirmed, no echo, or an echo that does not equal
    ``expected_hash`` exactly — returns the family's standard refusal
    envelope built by ``refuse()`` (an ActionError, an ActionResult, or a
    gate exception for the caller to raise). The caller keeps ownership of
    *whether* this run needs confirmation at all; this helper owns only the
    consent comparison, so no family can hand-roll (and drift) it again.

    Fail closed on a broken mint: an empty/absent ``expected_hash`` refuses
    unconditionally — a caller whose quote hash was never produced must not
    let a bare ``confirmed=true`` (or an echoed empty string) trivially
    match. Every current mint is non-empty; this guards the future caller.
    """
    if not expected_hash:
        return refuse()
    if confirmed and echoed_hash == expected_hash:
        return None
    return refuse()
