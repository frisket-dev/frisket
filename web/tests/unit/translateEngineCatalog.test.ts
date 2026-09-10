// Per-field fallback merge: a live catalog engine entry that is
// missing a field — a cached/version-skewed catalog without the new `language`/
// `allows_auto` declaration — must inherit the static fallback's value rather
// than wholly replacing it and silently losing the source control.

import { describe, expect, it } from 'vitest';

import type { ActionCatalogPayload, EngineOption } from '../../src/api/types';
import {
  TRANSLATE_ENGINE_FALLBACK,
  mergeEnginesWithFallback,
  translateEnginesFromCatalog,
} from '../../src/actions/translateEngineCatalog';

function catalogWith(engines: EngineOption[]): ActionCatalogPayload {
  return {
    schema_version: 'frisket.action_catalog.v2',
    actions: [
      {
        kind: 'map.translate',
        // Only ui_hints.engines is read by the resolver.
        ui_hints: { engines },
      } as ActionCatalogPayload['actions'][number],
    ],
    action_schema: {},
    error_schema: {},
    result_schema: {},
    receipt_schema: {},
    validation_result_schema: {},
  };
}

const fallbackLlm = TRANSLATE_ENGINE_FALLBACK.find((e) => e.id === 'llm')!;
const fallbackOpus = TRANSLATE_ENGINE_FALLBACK.find((e) => e.id === 'opus_mt')!;

describe('mergeEnginesWithFallback', () => {
  it('inherits an absent `language` field from the fallback twin', () => {
    const live: EngineOption = {
      id: 'llm',
      label: 'LLM (live label)',
      local: false,
      source: 'remote',
      available: true,
      // no `language` field — a stale catalog
    };
    const [merged] = mergeEnginesWithFallback([live], TRANSLATE_ENGINE_FALLBACK);
    // live values still win where present…
    expect(merged.label).toBe('LLM (live label)');
    expect(merged.available).toBe(true);
    // …and the missing declaration is inherited, not dropped.
    expect(merged.language).toEqual(fallbackLlm.language);
  });

  it('deep-merges a partial `language`: an absent nested field inherits the fallback', () => {
    const live: EngineOption = {
      id: 'opus_mt',
      label: 'Opus-MT',
      local: true,
      source: 'local',
      available: true,
      // a version-skewed declaration that omits `allows_auto`
      language: { mode: 'single', default: '', choices: null, detects: false } as EngineOption['language'],
    };
    const [merged] = mergeEnginesWithFallback([live], TRANSLATE_ENGINE_FALLBACK);
    // the nested no-auto flag is inherited from the fallback (false), not lost.
    expect(merged.language?.allows_auto).toBe(fallbackOpus.language?.allows_auto);
    expect(merged.language?.allows_auto).toBe(false);
    // present nested values still win.
    expect(merged.language?.detects).toBe(false);
  });

  it('passes a live-only engine (no fallback twin) through unchanged', () => {
    const live: EngineOption = { id: 'brand_new', label: 'New', local: false, source: 'remote', available: true };
    const [merged] = mergeEnginesWithFallback([live], TRANSLATE_ENGINE_FALLBACK);
    expect(merged).toEqual(live);
  });

  it('keeps the live set authoritative on which engines exist and their order', () => {
    const live: EngineOption[] = [
      { id: 'deepl', label: 'DeepL', local: false, source: 'remote', available: true },
      { id: 'llm', label: 'LLM', local: false, source: 'remote', available: true },
    ];
    const merged = mergeEnginesWithFallback(live, TRANSLATE_ENGINE_FALLBACK);
    expect(merged.map((e) => e.id)).toEqual(['deepl', 'llm']);
  });
});

describe('translateEnginesFromCatalog', () => {
  it('returns the static fallback when the catalog is absent', () => {
    expect(translateEnginesFromCatalog(null)).toBe(TRANSLATE_ENGINE_FALLBACK);
  });

  it('merges live entries over the fallback so a stale `language` is preserved', () => {
    const catalog = catalogWith([
      { id: 'llm', label: 'LLM', local: false, source: 'remote', available: true },
    ]);
    const [llm] = translateEnginesFromCatalog(catalog);
    expect(llm.language).toEqual(fallbackLlm.language);
  });
});
