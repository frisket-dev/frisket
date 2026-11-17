"""Share FastAPI's per-field pydantic TypeAdapters across app constructions.

``create_app`` registers several hundred routes, and FastAPI builds one
pydantic ``TypeAdapter`` (core-schema generation plus a compiled validator and
serializer) for every parameter and response field of every route, each time
an app is built. The suite builds thousands of apps. An adapter is a pure
function of the field's annotation, metadata, attributes and config, so
building each distinct one once per process and reusing it changes no
behaviour. A field whose inputs cannot be frozen into a hashable key is built
exactly as FastAPI builds it.
"""

from __future__ import annotations

from typing import Any, get_args, get_origin

from fastapi._compat import v2

FASTAPI_POST_INIT = v2.ModelField.__post_init__
_ADAPTERS: dict[Any, Any] = {}
_ANNOTATIONS: dict[int, tuple[Any, Any]] = {}
_SCALARS = frozenset({str, int, float, bool, bytes, type(None)})


def _frozen(value: Any) -> Any:
    """A hashable stand-in that is equal only for interchangeable inputs.

    Types and values are keyed with their type, so ``1``, ``1.0`` and ``True``
    stay distinct. Generic aliases are keyed by origin and ORDERED arguments:
    ``Union[A, B] == Union[B, A]`` in ``typing``, but the two produce
    differently ordered schemas.
    """
    if type(value) in _SCALARS:
        return (type(value), value)
    if isinstance(value, dict):
        return (dict, frozenset((key, _frozen(item)) for key, item in value.items()))
    if type(value) in (list, tuple):
        return (type(value), tuple(_frozen(item) for item in value))
    args = get_args(value)
    if args:
        return (get_origin(value), tuple(_frozen(arg) for arg in args))
    return (type(value), value)


def _frozen_annotation(annotation: Any) -> Any:
    """``_frozen`` memoized by identity: route annotations are module-level
    objects reused by every app, and some are large unions. The entry keeps
    the annotation alive, so its id cannot be reused by another object."""
    entry = _ANNOTATIONS.get(id(annotation))
    if entry is None or entry[0] is not annotation:
        entry = (annotation, _frozen(annotation))
        _ANNOTATIONS[id(annotation)] = entry
    return entry[1]


def _cached_post_init(field: Any) -> None:
    fields = v2.asdict(field.field_info)
    try:
        key = (
            _frozen_annotation(fields["annotation"]),
            _frozen(list(fields["metadata"])),
            _frozen(fields["attributes"]),
            _frozen(dict(field.config) if field.config is not None else None),
        )
        adapter = _ADAPTERS.get(key)
    except TypeError:
        FASTAPI_POST_INIT(field)
        return
    if adapter is None:
        FASTAPI_POST_INIT(field)
        _ADAPTERS[key] = field._type_adapter
    else:
        field._type_adapter = adapter


def install() -> None:
    v2.ModelField.__post_init__ = _cached_post_init
