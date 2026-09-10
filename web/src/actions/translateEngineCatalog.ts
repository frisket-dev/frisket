// THE shared TRANSLATE engine-catalog source: a third sibling to
// engineCatalog.ts (OCR) / transcribeEngineCatalog.ts (transcribe), following
// the same no-drift rule. The translate ACTION form (single-pick
// engine select) and the held Translate Compare tab (A24, multi-chip) both
// render the option set this module resolves — they may differ in INTERACTION
// but never in the option set, labels, tier metadata, or availability.
//
// Runtime truth is the backend action catalog (map.translate ui_hints.engines,
// assembled by _recipe_engines("translate") from provider-key + Opus-MT
// pair-install state). TRANSLATE_ENGINE_FALLBACK is the honest pre-catalog
// placeholder: every optional hosted/local engine is unavailable until live
// catalog truth confirms its key/pair state.
//
// Reuses engineCatalog.ts's already engine-kind-agnostic helpers (including the
// per-field fallback merge, `mergeEnginesWithFallback`) rather than duplicating
// them: the shared `makeEnginesFromCatalog` factory every catalog kind builds on.

import type { EngineOption, LanguageDeclaration } from '../api/types';
import { makeEnginesFromCatalog } from './engineCatalog';

export {
  tierForEngine,
  engineTierLabel,
  engineTierOptions,
  engineIsRemote,
  engineModelsSummary,
  mergeEnginesWithFallback,
} from './engineCatalog';

// The pre-catalog SOURCE-language declarations, mirroring the shared
// LanguageDeclaration the transcribe fallback carries (transcribeEngineCatalog.ts)
// and the backend's translate declarations (schemas/maps.py). The fallback
// carries a small common subset; live catalog truth
// (ui_hints.engines[*].language) supplies the full ~33-language roster.
const TRANSLATE_SOURCE_FALLBACK_CHOICES = [
  ['ar', 'Arabic'], ['zh', 'Chinese'], ['nl', 'Dutch'], ['en', 'English'],
  ['fr', 'French'], ['de', 'German'], ['hi', 'Hindi'], ['it', 'Italian'],
  ['ja', 'Japanese'], ['ko', 'Korean'], ['pt', 'Portuguese'], ['ru', 'Russian'],
  ['es', 'Spanish'], ['uk', 'Ukrainian'],
].map(([value, label]) => ({ value, label }));

// llm + hosted (deepl/google): one source OR Auto, engine reports detection.
const TRANSLATE_SINGLE_DETECTS: LanguageDeclaration = {
  mode: 'single',
  default: 'auto',
  choices: TRANSLATE_SOURCE_FALLBACK_CHOICES,
  detects: true,
  allows_auto: true,
};
// opus_mt: pair-based, NO language ID — requires an explicit source, cannot
// auto-detect. Choice-less until installed pairs populate it.
const TRANSLATE_OPUS_MT: LanguageDeclaration = {
  mode: 'single',
  default: '',
  choices: null,
  detects: false,
  allows_auto: false,
};
// hy_mt2: experimental local — auto-detects without reporting and consumes
// no source hint (its prompt names only the target), so it renders no source
// control (auto_only), mirroring the backend declaration (schemas/maps.py).
const TRANSLATE_HY_MT2: LanguageDeclaration = {
  mode: 'auto_only',
  default: 'auto',
  choices: null,
  detects: false,
};

export const TRANSLATE_ENGINE_FALLBACK: EngineOption[] = [
  { id: 'llm', label: 'LLM translation (uses your model)', tier: 'hosted', billable: true, available: false, error: 'Action catalog unavailable', language: TRANSLATE_SINGLE_DETECTS },
  {
    id: 'deepl',
    label: 'DeepL API (remote)',
    tier: 'hosted',
    billable: true,
    available: false,
    error: 'Action catalog unavailable',
    language: TRANSLATE_SINGLE_DETECTS,
  },
  {
    id: 'google_translate',
    label: 'Google Cloud Translation (remote)',
    tier: 'hosted',
    billable: true,
    available: false,
    error: 'Action catalog unavailable',
    language: TRANSLATE_SINGLE_DETECTS,
  },
  {
    id: 'opus_mt',
    label: 'Opus-MT local translation (per-language-pair)',
    tier: 'local',
    available: false,
    error: 'Action catalog unavailable',
    language: TRANSLATE_OPUS_MT,
  },
  {
    id: 'hy_mt2',
    label: 'Hy-MT2 local translation (experimental)',
    tier: 'local',
    available: false,
    error: 'Action catalog unavailable',
    language: TRANSLATE_HY_MT2,
  },
];

/** Resolve the translate engine option set from a loaded action catalog.
 *  Returns the shared fallback when the catalog is absent or has no hints; when
 *  it has hints, each live engine is merged per-field over the fallback twin
 *  (`mergeEnginesWithFallback`, re-exported above from engineCatalog.ts). */
export const translateEnginesFromCatalog = makeEnginesFromCatalog(
  'map.translate',
  TRANSLATE_ENGINE_FALLBACK,
);
