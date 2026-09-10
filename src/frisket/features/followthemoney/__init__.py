"""FollowTheMoney adapter boundary for investigative rowset projections.

Everything below requires the ``entities`` extra: followthemoney pulls in
normality -> pyicu, a native extension whose official PyPI release has no
wheels on any platform, so it stays opt-in rather than a base dependency.
Importing this PACKAGE must never
crash a base install -- ``entities_available()`` is the single source of
truth callers check before touching any of the real submodules below.

``ENTITIES_AVAILABLE`` is determined by an ACTUAL import attempt of the real
submodules below, not a
cheap ``find_spec`` presence check -- a package can be discoverable
(``find_spec`` succeeds) while still failing to load (missing/broken
``normality``, ``rigour``, or the native ``pyicu`` extension underneath), and
a presence-only check would have reported that broken state as available,
then crashed the first real touch. The import attempt below is guarded by
each submodule's own ``_sdk_import.require_followthemoney_sdk()`` (which now
catches broadly, not just ``ImportError``), so ANY failure mode collapses to
the same clean ``ImportError`` and this package's own import can never crash.

A *direct* import of a submodule
(``frisket.features.followthemoney.adapter``, ``.import_planner``,
``.mapping``, ``.schema_catalog``) or of a name this package would normally
re-export (``from frisket.features.followthemoney import validate_entity``) must ALSO
raise a clear, actionable error -- not a bare ``ModuleNotFoundError`` or
Python's generic "cannot import name". The submodules guard their own
top-level SDK import via ``_sdk_import.require_followthemoney_sdk()``; this
``__init__`` covers the re-export case via ``__getattr__`` (PEP 562) below.
"""

from __future__ import annotations

from frisket.features.followthemoney._sdk_import import ENTITIES_EXTRA_REMEDIATION

# Names this package re-exports ONLY when the entities extra is installed
# AND actually loads. Accessing one of these while unavailable raises a
# clear ImportError via __getattr__ below, instead of Python's generic
# "cannot import name ...".
_REAL_EXPORTS = frozenset(
    {
        "FOLLOWTHEMONEY_ENTITY_SCHEMA_VERSION",
        "FOLLOWTHEMONEY_EXPORT_PACKAGE_SCHEMA_VERSION",
        "FOLLOWTHEMONEY_MAPPING_SCHEMA_VERSION",
        "FOLLOWTHEMONEY_MATCH_REVIEW_PLAN_SCHEMA_VERSION",
        "FOLLOWTHEMONEY_MATCHING_SCHEMA_VERSION",
        "FOLLOWTHEMONEY_SOURCE_REFS_SCHEMA_VERSION",
        "FOLLOWTHEMONEY_VALIDATION_REPORT_SCHEMA_VERSION",
        "SUPPORTED_SCHEMA_PRESETS",
        "FollowTheMoneyAdapterError",
        "build_followthemoney_export_package",
        "deterministic_row_ref_id",
        "followthemoney_rowset_key",
        "get_property_metadata",
        "get_schema_metadata",
        "map_rowset_to_entities",
        "match_followthemoney_entities",
        "normalize_property_values",
        "parse_entities_jsonl",
        "plan_followthemoney_match_review_sheet",
        "serialize_entities_jsonl",
        "supported_schema_presets",
        "validate_entity",
    }
)

_import_error: ImportError | None = None

try:
    from frisket.features.followthemoney.adapter import (
        FOLLOWTHEMONEY_ENTITY_SCHEMA_VERSION,
        FollowTheMoneyAdapterError,
        parse_entities_jsonl,
        serialize_entities_jsonl,
        validate_entity,
    )
    from frisket.features.followthemoney.exporters import (
        FOLLOWTHEMONEY_EXPORT_PACKAGE_SCHEMA_VERSION,
        FOLLOWTHEMONEY_SOURCE_REFS_SCHEMA_VERSION,
        FOLLOWTHEMONEY_VALIDATION_REPORT_SCHEMA_VERSION,
        build_followthemoney_export_package,
        followthemoney_rowset_key,
    )
    from frisket.features.followthemoney.mapping import (
        FOLLOWTHEMONEY_MAPPING_SCHEMA_VERSION,
        deterministic_row_ref_id,
        map_rowset_to_entities,
        normalize_property_values,
    )
    from frisket.features.followthemoney.matching import (
        FOLLOWTHEMONEY_MATCH_REVIEW_PLAN_SCHEMA_VERSION,
        FOLLOWTHEMONEY_MATCHING_SCHEMA_VERSION,
        match_followthemoney_entities,
        plan_followthemoney_match_review_sheet,
    )
    from frisket.features.followthemoney.schema_catalog import (
        SUPPORTED_SCHEMA_PRESETS,
        get_property_metadata,
        get_schema_metadata,
        supported_schema_presets,
    )
except ImportError as exc:
    ENTITIES_AVAILABLE = False
    _import_error = exc
else:
    ENTITIES_AVAILABLE = True


def entities_available() -> tuple[bool, str | None]:
    """Whether the ``entities`` extra (followthemoney/normality/pyicu) is
    installed AND actually loads: safe to call from anywhere (diagnostics,
    the graph-neighborhood route, the bundled ``frisket.ftm`` plugin) before
    touching this package's real submodules. Never raises."""
    if ENTITIES_AVAILABLE:
        return True, None
    return False, (
        "FollowTheMoney entity support is not installed. Install with "
        f"{ENTITIES_EXTRA_REMEDIATION}."
    )


if ENTITIES_AVAILABLE:
    # Literal names (not a `*_REAL_EXPORTS` splat) so static analysis can see
    # each import above is used via re-export; the assertion below is the
    # drift guard that keeps this list and `_REAL_EXPORTS` in lockstep.
    __all__ = [
        "ENTITIES_AVAILABLE",
        "ENTITIES_EXTRA_REMEDIATION",
        "entities_available",
        "FOLLOWTHEMONEY_ENTITY_SCHEMA_VERSION",
        "FOLLOWTHEMONEY_EXPORT_PACKAGE_SCHEMA_VERSION",
        "FOLLOWTHEMONEY_MAPPING_SCHEMA_VERSION",
        "FOLLOWTHEMONEY_MATCH_REVIEW_PLAN_SCHEMA_VERSION",
        "FOLLOWTHEMONEY_MATCHING_SCHEMA_VERSION",
        "FOLLOWTHEMONEY_SOURCE_REFS_SCHEMA_VERSION",
        "FOLLOWTHEMONEY_VALIDATION_REPORT_SCHEMA_VERSION",
        "SUPPORTED_SCHEMA_PRESETS",
        "FollowTheMoneyAdapterError",
        "build_followthemoney_export_package",
        "deterministic_row_ref_id",
        "followthemoney_rowset_key",
        "get_property_metadata",
        "get_schema_metadata",
        "map_rowset_to_entities",
        "match_followthemoney_entities",
        "normalize_property_values",
        "parse_entities_jsonl",
        "plan_followthemoney_match_review_sheet",
        "serialize_entities_jsonl",
        "supported_schema_presets",
        "validate_entity",
    ]
    assert (
        set(__all__)
        - {
            "ENTITIES_AVAILABLE",
            "ENTITIES_EXTRA_REMEDIATION",
            "entities_available",
        }
        == _REAL_EXPORTS
    ), "__all__ and _REAL_EXPORTS drifted apart"
else:
    __all__ = [
        "ENTITIES_AVAILABLE",
        "ENTITIES_EXTRA_REMEDIATION",
        "entities_available",
    ]

    def __getattr__(name: str) -> None:
        if name in _REAL_EXPORTS:
            _, err = entities_available()
            raise ImportError(
                f"frisket.features.followthemoney.{name} requires the entities extra: {err}"
            )
        raise AttributeError(
            f"module 'frisket.features.followthemoney' has no attribute {name!r}"
        )
