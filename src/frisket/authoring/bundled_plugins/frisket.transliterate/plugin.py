"""Backend handler for the bundled ``frisket.transliterate`` plugin: a
deterministic column-transform action that converts non-Latin text
(Cyrillic, CJK, Greek, Arabic, ...) to a Latin-script approximation. The
output is a lossy ASCII approximation, not a linguistically faithful
transliteration -- good for search/sort/dedup, not a substitute for a real
transliteration standard.

Engine selection is runtime-automatic and probes REAL importability, not
just discoverability: a package can be `find_spec`-visible (installed
metadata present) while still failing to load (missing native shared
library, broken ICU data, ...), so the probe below does a real `import
icu` + `Transliterator.createInstance(...)` and caches whichever outcome
actually happens -- never just checks that the module COULD exist.

- **``icu`` (PyICU's ``icu.Transliterator``, "Any-Latin; Latin-ASCII")** --
  broader rule-based script coverage than the pure-Python fallback. PyICU is
  a native extension whose official PyPI release has no wheels on any
  platform, so it is never a hard dependency here; it rides in for free once
  the ``entities`` extra is installed (followthemoney -> normality -> pyicu,
  decision 15), or if the operator installed it independently.
- **``text_unidecode`` (pure Python)** -- the always-available fallback.
  Deliberately NOT a new dependency: ``text-unidecode`` is a direct base
  dependency (promoted from a transitive one this plugin relied on --
  ``braintrust`` -> ``python-slugify`` -> ``text-unidecode`` -- so it can
  never silently disappear on a future dependency bump), so this plugin
  needs nothing new and works out of the box with zero extras.

Neither engine is gated behind the ``entities`` extra -- unlike the
FollowTheMoney surfaces, this plugin has a real base-install-only path, so
gating it would throw away working functionality for no reason.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from frisket.actions.core import ActionCategory, RowScope, action, column_transform
from frisket.actions.types import ActionParams, ColumnRef, RowResult, Rows
from frisket.plugins.sdk import Plugin

ENGINE_ICU = "icu"
ENGINE_TEXT_UNIDECODE = "text_unidecode"

_ICU_TRANSLITERATOR_ID = "Any-Latin; Latin-ASCII"

# Probe-result cache. `False` = not yet probed; `None` = probed and the real
# import/construction failed (discoverable-but-unloadable OR truly absent);
# any other value is the constructed `icu.Transliterator` instance itself --
# probing and building the instance are the SAME step, so a successful probe
# never pays a second construction cost.
_icu_transliterator_cache: Any = False


def _icu_transliterator() -> Any:
    """Real import + construction probe, cached. Returns the constructed
    ``icu.Transliterator`` instance, or ``None`` if PyICU is absent OR
    present-but-broken (e.g. installed package metadata with no working
    native library / ICU data -- a real failure mode `find_spec` alone
    cannot see, since it only checks that a module COULD be imported)."""
    global _icu_transliterator_cache
    if _icu_transliterator_cache is False:
        try:
            import icu

            _icu_transliterator_cache = icu.Transliterator.createInstance(
                _ICU_TRANSLITERATOR_ID
            )
        except Exception:  # noqa: BLE001 -- any failure means "use the fallback"
            _icu_transliterator_cache = None
    return _icu_transliterator_cache


def icu_available() -> bool:
    """Whether PyICU actually works right now: imported AND its
    transliterator constructed successfully -- not just importable."""
    return _icu_transliterator() is not None


def active_engine() -> str:
    """Which engine ``transliterate()`` uses by default right now."""
    return ENGINE_ICU if icu_available() else ENGINE_TEXT_UNIDECODE


def _icu_transliterate(text: str) -> str:
    transliterator = _icu_transliterator()
    if transliterator is None:
        raise ValueError(
            f"engine={ENGINE_ICU!r} was requested but PyICU is not usable "
            "(either not installed, or installed but failed to load -- "
            "pip install 'frisket[entities]', or `pip install pyicu` directly)"
        )
    return str(transliterator.transliterate(text))


def _text_unidecode_transliterate(text: str) -> str:
    import text_unidecode

    return text_unidecode.unidecode(text)


def transliterate(text: str, *, engine: str | None = None) -> tuple[str, str]:
    """Transliterate ``text`` to a Latin-script approximation.

    Returns ``(latin_text, engine_used)``. With no ``engine`` argument (the
    default/automatic path), an ICU that is discoverable but fails to
    actually load falls back to ``text_unidecode`` silently-but-honestly
    (the ``engine`` return value says which one ran) -- ``active_engine()``
    already reflects the real probe outcome, never a guess. Passing an
    explicit ``engine`` FORCES that engine and raises ``ValueError`` if it
    isn't usable, since an explicit request is a promise the caller can
    act on, not a default to silently downgrade."""
    chosen = engine or active_engine()
    if chosen == ENGINE_ICU:
        return _icu_transliterate(text), ENGINE_ICU
    if chosen == ENGINE_TEXT_UNIDECODE:
        return _text_unidecode_transliterate(text), ENGINE_TEXT_UNIDECODE
    raise ValueError(f"unknown transliteration engine: {chosen!r}")


class TransliterateParams(ActionParams):
    text: ColumnRef[str]


class TransliterateOutput(BaseModel):
    latin_text: str | None


def transliterate_column(
    params: TransliterateParams, rows: Rows
) -> dict[int, RowResult[TransliterateOutput]]:
    results: dict[int, RowResult[TransliterateOutput]] = {}
    for row_id, row in rows.items():
        text = params.text.read(row)
        latin_text = None if text is None else transliterate(str(text))[0]
        results[row_id] = RowResult(output=TransliterateOutput(latin_text=latin_text))
    return results


TRANSLITERATE = action(
    name="transliterate",
    title="Transliterate to Latin script",
    description="Convert text from other writing systems to Latin script.",
    category=ActionCategory.TEXT,
    row_scope=RowScope.ALL_ROWS,
    examples=(TransliterateParams(text="text"),),
    run=column_transform(transliterate_column),
)

plugin = Plugin(
    id="frisket.transliterate",
    version="1.0.0",
    capabilities=["plugin:trusted_local_backend"],
    actions=(TRANSLITERATE,),
)
