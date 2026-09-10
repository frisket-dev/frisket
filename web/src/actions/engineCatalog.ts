// THE shared engine-catalog source (no-drift): the OCR ACTION form (single-pick
// engine select in the drawer) and the OCR COMPARE tab (multi-chip bake-off) both render the
// option set this module resolves. The two surfaces may differ in INTERACTION
// (pick-one vs add-many) but never in the option set, labels, local/remote
// metadata, availability, or cost hints — all of that lives here.
//
// Runtime truth is the backend action catalog (media.ocr ui_hints.engines,
// assembled server-side from the OCR engine registry + provider key state);
// OCR_ENGINE_FALLBACK is the honest pre-catalog placeholder both surfaces
// share when the catalog has not loaded or failed (remote engines are marked
// unavailable there so nothing can run on them without live catalog truth).
//
// The list itself is generated (the OCR-list twin of modelCatalog.generated.ts):
// scripts/dev/sync_ocr_engine_catalog.py derives it from the backend's
// OCR_ENGINE_TABLE + OCR_VISION_ENGINES (src/frisket/server/action_catalog_hints.py),
// and tests/server/test_ocr_engine_catalog_sync.py's `--check` run fails CI if
// this drifts. The guard prevents a removed backend engine from remaining
// selectable in the UI. Edit the backend roster, then run the sync script;
// never hand-edit engineCatalog.generated.ts.

import type { ActionCatalogPayload, EngineOption, EngineTier } from '../api/types';
import { OCR_ENGINE_FALLBACK_GENERATED } from './engineCatalog.generated';

export const OCR_ENGINE_FALLBACK: EngineOption[] = OCR_ENGINE_FALLBACK_GENERATED;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Generic recursive DEFAULT merge: `override`'s present values win; keys ABSENT
 *  on `override` inherit `base`'s. Recurses into nested plain objects (so a
 *  version-skewed live `language` missing `allows_auto` still inherits it);
 *  arrays and scalars are taken wholesale from `override` when present. Not
 *  field-by-field — it merges whatever shape the two objects carry, so new
 *  EngineOption fields (`license`, …) inherit automatically. */
function mergeDefaults<T>(base: T, override: T): T {
  if (!isPlainObject(base) || !isPlainObject(override)) return override;
  const out: Record<string, unknown> = { ...base };
  for (const [key, value] of Object.entries(override)) {
    out[key] = key in base ? mergeDefaults((base as Record<string, unknown>)[key], value) : value;
  }
  return out as T;
}

/** Merge each LIVE catalog engine with the matching static fallback per-field so
 *  a live entry missing a field (a cached/version-skewed catalog without a
 *  newly-added declaration) inherits the fallback's rather than silently
 *  dropping it. Live entries with no fallback twin pass through; the live set
 *  stays authoritative on WHICH engines exist and in what order. Applies to
 *  every catalog kind — OCR/transcribe carry the identical staleness risk. */
export function mergeEnginesWithFallback(
  live: EngineOption[],
  fallback: EngineOption[],
): EngineOption[] {
  const fallbackById = new Map(fallback.map((engine) => [engine.id, engine]));
  return live.map((entry) => {
    const base = fallbackById.get(entry.id);
    return base ? mergeDefaults(base, entry) : entry;
  });
}

/** THE factory every catalog-kind's `*_ENGINE_FALLBACK` / `*EnginesFromCatalog`
 *  pair is built from: OCR, transcribe, and translate each independently
 *  wrote the identical "find the action by kind, read ui_hints.engines, fall
 *  back to the static list" resolver. Returns the exact `fallback` reference
 *  (not a copy) when the catalog has no hints, so callers relying on
 *  referential identity in that branch (e.g. `toBe(FALLBACK)` in tests) keep
 *  working. */
export function makeEnginesFromCatalog(
  kind: string,
  fallback: EngineOption[],
): (catalog: ActionCatalogPayload | null | undefined) => EngineOption[] {
  return (catalog) => {
    const action = catalog?.actions.find((entry) => entry.kind === kind);
    const engines = action?.ui_hints.engines;
    return engines?.length ? mergeEnginesWithFallback(engines, fallback) : fallback;
  };
}

/** Resolve the OCR engine option set from a loaded action catalog. Returns the
 *  shared fallback when the catalog is absent or carries no engine hints. */
export const ocrEnginesFromCatalog = makeEnginesFromCatalog('media.ocr', OCR_ENGINE_FALLBACK);

/** An engine's tier (three-tier vocabulary: local | sidecar | hosted). */
export function tierForEngine(engine: EngineOption): EngineTier {
  return engine.tier ?? 'hosted';
}

const ENGINE_TIER_LABELS: Record<EngineTier, string> = {
  local: 'Local',
  sidecar: 'Sidecar',
  hosted: 'Hosted',
};

export function engineTierLabel(tier: EngineTier): string {
  return ENGINE_TIER_LABELS[tier] ?? tier;
}

/** Engines grouped by tier with availability counts — the action form's tier
 *  chips and any other tier-aware selector render from this one grouping. */
export function engineTierOptions(engines: EngineOption[] | undefined): Array<{
  tier: EngineTier;
  engines: EngineOption[];
  availableCount: number;
}> {
  const byTier = new Map<EngineTier, EngineOption[]>();
  for (const engine of engines ?? []) {
    const tier = tierForEngine(engine);
    const existing = byTier.get(tier);
    if (existing) {
      existing.push(engine);
    } else {
      byTier.set(tier, [engine]);
    }
  }
  return [...byTier.entries()].map(([tier, tierEngines]) => ({
    tier,
    engines: tierEngines,
    availableCount: tierEngines.filter((engine) => engine.available !== false).length,
  }));
}

/** The reason an unavailable engine can't run, for display next to its
 *  disabled state (never a bare disabled control). The catalog's `error`
 *  string is the backend's own reason —
 *  policy ("Network access is disabled…") or config ("no API key…",
 *  "sidecar not configured") — passed through verbatim; the generic
 *  'Unavailable' only covers a catalog entry that carried no reason at all.
 *  Returns null for available engines. */
export function engineUnavailableReason(engine: EngineOption | undefined): string | null {
  if (!engine || engine.available !== false) return null;
  return engine.error ?? 'Unavailable';
}

/** Billable engines bill per page/call and need the explicit allow-remote
 *  confirm; any hosted-tier engine leaves the operator's infrastructure. */
export function engineIsRemote(engine: EngineOption | undefined): boolean {
  if (!engine) return false;
  if (engine.billable) return true;
  if (engine.id.includes('/')) return true;
  return tierForEngine(engine) === 'hosted';
}

export function engineModelsSummary(engine: EngineOption | undefined): string {
  return engine?.models?.length ? `Models: ${engine.models.join(', ')}` : '';
}
