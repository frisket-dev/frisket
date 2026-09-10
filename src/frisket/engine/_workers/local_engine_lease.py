"""Process-global admission gate for local heavyweight engine children.

This lease primitive is engine-neutral: it is keyed by engine name so admission
and poisoning are per engine, and it is shared by every local heavyweight
engine (Parakeet transcription and RapidOCR today; a GPU transcription session
next). It previously lived inside ``parakeet_session`` and was cross-imported by
the OCR op — a surprising coupling that a future Parakeet refactor could have
silently broken. It now lives in this neutral module, and every caller
(Parakeet and RapidOCR alike) imports it from here directly.
"""

from __future__ import annotations

import threading


class LocalEngineLeaseBusy(RuntimeError):
    pass


class _EngineLeaseState:
    def __init__(self) -> None:
        self.owner: object | None = None
        self.poisoned = False


class _LeaseState:
    """Process-global admission gate for local heavyweight engine children.

    Keyed by engine name so admission — and poisoning — are per engine: one
    run-owned process resource per engine at a time, and a poisoned engine bars
    only itself.
    Parakeet and RapidOCR lease independently today; other local children run
    ungated, so there is deliberately no cross-engine capacity budget.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.engines: dict[str, _EngineLeaseState] = {}

    def state_for(self, engine: str) -> _EngineLeaseState:
        return self.engines.setdefault(engine, _EngineLeaseState())


_LEASE_STATE = _LeaseState()


def _busy_message(engine: str) -> str:
    # Names the engine but deliberately not any specific kind of work (e.g.
    # "transcription"): the running session may be doing different work.
    return f"another local {engine} session is running; retry after it closes"


class LocalEngineLease:
    def __init__(self, engine: str, owner: object) -> None:
        self._engine = engine
        self._owner = owner
        self._finished = False

    def release(self) -> None:
        if self._finished:
            return
        with _LEASE_STATE.lock:
            state = _LEASE_STATE.engines.get(self._engine)
            if state is None or state.owner is not self._owner:
                raise RuntimeError(f"local {self._engine} lease owner mismatch")
            state.owner = None
            self._finished = True

    def poison(self) -> None:
        """Bar this engine from admission; other engines are unaffected."""

        if self._finished:
            return
        with _LEASE_STATE.lock:
            state = _LEASE_STATE.engines.get(self._engine)
            if state is None or state.owner is not self._owner:
                raise RuntimeError(f"local {self._engine} lease owner mismatch")
            # Deliberately keep ``poisoned`` set forever: process restart is
            # the only recovery from an unprovable survivor of this engine.
            state.poisoned = True
            state.owner = None
            self._finished = True


def acquire_local_engine_lease(engine: str) -> LocalEngineLease:
    with _LEASE_STATE.lock:
        state = _LEASE_STATE.state_for(engine)
        if state.poisoned:
            raise LocalEngineLeaseBusy(
                f"local {engine} admission is unavailable until process restart"
            )
        if state.owner is not None:
            raise LocalEngineLeaseBusy(_busy_message(engine))
        owner = object()
        state.owner = owner
    return LocalEngineLease(engine, owner)


def _reset_lease_state_for_tests() -> None:
    """Test-only reset; never call while a real child may exist."""

    global _LEASE_STATE
    _LEASE_STATE = _LeaseState()
