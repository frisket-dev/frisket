"""Each HTTP endpoint call is one unit of work on the project store."""

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from fastapi.routing import APIRoute

from frisket.engine.store.transactions import roll_back_unfinished_work

_P = ParamSpec("_P")
_R = TypeVar("_R")


def ends_unit_of_work(
    call: Callable[_P, _R], *, unit: str | None = None
) -> Callable[_P, _R]:
    """``call`` as one unit of work: work it leaves uncommitted is rolled back.

    The rollback runs on the thread that ran ``call``, because project
    connections are per thread. Work handed to another thread is its own
    unit of work there; ``unit`` names it in the log.
    """
    name = unit or getattr(call, "__qualname__", None) or repr(call)

    @wraps(call)
    def run(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return call(*args, **kwargs)
        finally:
            roll_back_unfinished_work(unit=name)

    return run


def _ends_async_unit_of_work(
    call: Callable[_P, Awaitable[_R]], *, unit: str
) -> Callable[_P, Awaitable[_R]]:
    @wraps(call)
    async def run(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return await call(*args, **kwargs)
        finally:
            roll_back_unfinished_work(unit=unit)

    return run


class UnitOfWorkRoute(APIRoute):
    """A route whose endpoint call ends by rolling back work it left uncommitted.

    A ``def`` endpoint runs on a threadpool thread, one call at a time, so
    any transaction open on that thread when the call returns is the call's
    own. An ``async def`` endpoint runs on the event loop, which interleaves
    requests on one thread; the rollback there is sound because no project
    transaction stays open across an ``await`` (one that did would already
    share its transaction with every request the loop ran meanwhile). A
    streaming endpoint keeps running after its call returns and is left as
    it is.

    A project transaction must not span an ``await`` on the server loop: the
    rollback at the end of an ``async def`` endpoint covers every project
    connection of the loop thread. ``unfinished_work_rolled_back`` logged
    for a request that succeeded means some coroutine holds one.
    """

    def get_route_handler(self):
        dependant = self.dependant
        call = dependant.call
        if call is not None and not (
            dependant.is_gen_callable or dependant.is_async_gen_callable
        ):
            unit = f"{' '.join(sorted(self.methods))} {self.path}"
            dependant.call = (
                _ends_async_unit_of_work(call, unit=unit)
                if dependant.is_coroutine_callable
                else ends_unit_of_work(call, unit=unit)
            )
        return super().get_route_handler()
