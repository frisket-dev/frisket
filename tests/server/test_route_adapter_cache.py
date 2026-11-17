"""The suite-wide FastAPI adapter cache must be invisible to the apps it serves."""

from __future__ import annotations

import json
from typing import Literal, Union

from fastapi._compat import v2
from fastapi.routing import APIRoute

import route_adapter_cache
from frisket.server.app import create_app


def _response_adapters(app) -> dict[tuple[str, frozenset[str]], object]:
    return {
        (route.path, frozenset(route.methods)): route.response_field._type_adapter
        for route in app.routes
        if isinstance(route, APIRoute) and route.response_field is not None
    }


def test_the_cache_is_installed_for_the_whole_suite():
    # Without it every create_app repeats full pydantic schema generation; a
    # FastAPI upgrade that moves this hook must fail here, not slow the suite.
    assert v2.ModelField.__post_init__ is route_adapter_cache._cached_post_init


def test_cached_adapters_produce_the_same_contract_as_fresh_ones(tmp_path, monkeypatch):
    first = create_app(tmp_path / "first")
    cached = create_app(tmp_path / "cached")
    shared = _response_adapters(first).items() & _response_adapters(cached).items()
    assert shared, "a second app must reuse the first app's adapters"

    monkeypatch.setattr(
        v2.ModelField, "__post_init__", route_adapter_cache.FASTAPI_POST_INIT
    )
    fresh = create_app(tmp_path / "fresh")
    assert not (_response_adapters(fresh).items() & _response_adapters(cached).items())
    assert json.dumps(cached.openapi(), sort_keys=True) == json.dumps(
        fresh.openapi(), sort_keys=True
    )


def test_keys_keep_union_order_and_value_types_apart():
    frozen = route_adapter_cache._frozen
    # typing treats these unions as equal, but their schemas list members in
    # declaration order.
    assert Union[int, str] == Union[str, int]
    assert frozen(Union[int, str]) != frozen(Union[str, int])
    assert frozen(Literal[1]) != frozen(Literal[True])
    assert frozen({"default": 1}) != frozen({"default": 1.0})
    assert frozen([1, "a"]) == frozen([1, "a"])
