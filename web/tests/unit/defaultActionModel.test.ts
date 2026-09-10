// Bug: the action form's model picker defaulted every fresh launch to
// MODEL_OPTIONS[0] (a Gemini model, see modelCatalog.generated.ts) regardless
// of which provider the workspace actually configured — a workspace with
// only an Anthropic key got a Gemini model preselected, then the run failed
// (or silently asked for the wrong credential). defaultActionModel is the
// fix: it walks MODEL_OPTIONS and returns the first entry whose provider the
// live /api/providers catalog reports as usable, or null when nothing is.
import { describe, expect, it } from 'vitest';

import {
  MODEL_OPTIONS,
  defaultActionModel,
  defaultCopilotModel,
  modelProviderId,
  providerIsUsable,
} from '../../src/actions/model';
import type { LocalHttpEndpointEntry, LocalProviderCatalog, LocalProviderEntry, PlatformLocalProviderEntry } from '../../src/api/types';

function entry(overrides: Partial<PlatformLocalProviderEntry> & Pick<PlatformLocalProviderEntry, 'id' | 'label' | 'kind'>): PlatformLocalProviderEntry {
  return { models: [], configured: false, source: null, hint: null, ...overrides };
}

function localEntry(overrides: Partial<LocalHttpEndpointEntry> & Pick<LocalHttpEndpointEntry, 'endpoint_id' | 'label'>): LocalHttpEndpointEntry {
  return {
    kind: 'local_http',
    read_only: false,
    models: [],
    reachable: false,
    origin: 'http://127.0.0.1:11434',
    authority: 'instance',
    source: 'stored',
    detail: null,
    protocol: 'unknown',
    auth_status: 'unknown',
    token_configured: false,
    provisioning_token_configured: false,
    edge_auth: false,
    pull_enabled: false,
    ...overrides,
  };
}

function catalogOf(providers: LocalProviderEntry[]): LocalProviderCatalog {
  return { schemaVersion: 'frisket.providers.v1', tier: 'local', providers };
}

describe('defaultActionModel', () => {
  it('only Anthropic configured -> preselects an Anthropic model, not MODEL_OPTIONS[0] (Gemini)', () => {
    // Sanity-check the bug's premise: the built-in catalog's first entry is
    // a Gemini model, the exact wrong default this workspace must not get.
    expect(modelProviderId(MODEL_OPTIONS[0].id)).toBe('gemini');

    const catalog = catalogOf([
      entry({ id: 'gemini', label: 'Gemini', kind: 'platform_api', configured: false }),
      entry({ id: 'anthropic', label: 'Anthropic', kind: 'platform_api', configured: true }),
      entry({ id: 'openai', label: 'OpenAI', kind: 'platform_api', configured: false }),
      localEntry({ endpoint_id: 'desktop', label: 'Local server' }),
    ]);

    const picked = defaultActionModel(catalog);
    expect(picked).not.toBeNull();
    expect(modelProviderId(picked as string)).toBe('anthropic');
  });

  it('nothing configured -> null (the no-provider state), never a Gemini model presented as ready', () => {
    const catalog = catalogOf([
      entry({ id: 'gemini', label: 'Gemini', kind: 'platform_api', configured: false }),
      entry({ id: 'anthropic', label: 'Anthropic', kind: 'platform_api', configured: false }),
      entry({ id: 'openai', label: 'OpenAI', kind: 'platform_api', configured: false }),
      localEntry({ endpoint_id: 'desktop', label: 'Local server' }),
    ]);

    expect(defaultActionModel(catalog)).toBeNull();
    expect(defaultActionModel(null)).toBeNull();
  });

  it('ollama-only (reachable, has installed models) -> the ACTUALLY installed ollama model, not the static qwen3:8b placeholder', () => {
    // Deliberately not qwen3:8b: MODEL_OPTIONS carries exactly that id as
    // its one static Ollama entry, so a fixture that installs it would pass
    // even from the pre-fix bug (matching ollama by provider id alone,
    // never reading the live installed list -- the phantom-default D3
    // bug). llama3.1 here proves the fix reads `models` off the live
    // catalog entry, mirroring defaultCopilotModel's ollama branch.
    const catalog = catalogOf([
      entry({ id: 'gemini', label: 'Gemini', kind: 'platform_api', configured: false }),
      entry({ id: 'anthropic', label: 'Anthropic', kind: 'platform_api', configured: false }),
      entry({ id: 'openai', label: 'OpenAI', kind: 'platform_api', configured: false }),
      localEntry({
        endpoint_id: 'desktop',
        label: 'Local server',
        reachable: true,
        models: [{ id: 'ollama/@desktop/llama3.1', label: 'llama3.1', price: null, local: true }],
      }),
    ]);

    const picked = defaultActionModel(catalog);
    expect(picked).toBe('ollama/@desktop/llama3.1');
  });

  it('uses a reachable endpoint when another endpoint is offline', () => {
    const catalog = catalogOf([
      localEntry({
        endpoint_id: 'offline',
        label: 'Local server',
        reachable: false,
        models: [],
      }),
      localEntry({
        endpoint_id: 'local-a1b2c3d4e5f6',
        label: 'LM Studio',
        reachable: true,
        models: [{
          id: 'ollama/@local-a1b2c3d4e5f6/qwen',
          label: 'qwen',
          price: null,
          local: true,
        }],
      }),
    ]);

    expect(defaultActionModel(catalog)).toBe('ollama/@local-a1b2c3d4e5f6/qwen');
    expect(defaultCopilotModel(catalog)).toBe('ollama/@local-a1b2c3d4e5f6/qwen');
  });

  it('ollama-only but only qwen3:8b is NOT installed (llama3.1 is) -> never falls back to the static qwen3:8b id', () => {
    const catalog = catalogOf([
      entry({ id: 'gemini', label: 'Gemini', kind: 'platform_api', configured: false }),
      localEntry({
        endpoint_id: 'desktop',
        label: 'Local server',
        reachable: true,
        models: [{ id: 'ollama/@desktop/llama3.1', label: 'llama3.1', price: null, local: true }],
      }),
    ]);

    expect(defaultActionModel(catalog)).not.toBe('ollama/@desktop/qwen3:8b');
    expect(defaultActionModel(catalog)).toBe('ollama/@desktop/llama3.1');
  });

  it('ollama reachable but with zero installed models is not usable (mirrors defaultCopilotModel\'s rule)', () => {
    const catalog = catalogOf([
      entry({ id: 'gemini', label: 'Gemini', kind: 'platform_api', configured: false }),
      localEntry({ endpoint_id: 'desktop', label: 'Local server', reachable: true }),
    ]);
    expect(providerIsUsable(catalog.providers[1])).toBe(false);
    expect(defaultActionModel(catalog)).toBeNull();
  });

  it('openrouter-only uses the curated open-weight model, never null/Gemini', () => {
    expect(MODEL_OPTIONS).toContainEqual({
      id: 'openrouter/qwen/qwen3-8b',
      label: 'Qwen 3 8B — open weights; requests go to OpenRouter',
    });
    const catalog = catalogOf([
      entry({ id: 'gemini', label: 'Gemini', kind: 'platform_api', configured: false }),
      entry({ id: 'anthropic', label: 'Anthropic', kind: 'platform_api', configured: false }),
      entry({ id: 'openai', label: 'OpenAI', kind: 'platform_api', configured: false }),
      entry({
        id: 'openrouter',
        label: 'OpenRouter',
        kind: 'platform_api',
        configured: true,
        models: [{ id: 'openrouter/anthropic/claude-3.5-sonnet', label: 'Claude 3.5 Sonnet', price: null }],
      }),
    ]);

    const picked = defaultActionModel(catalog);
    expect(picked).toBe('openrouter/qwen/qwen3-8b');
  });

  it('openrouter configured with an empty live list uses the curated model', () => {
    const catalog = catalogOf([
      entry({ id: 'gemini', label: 'Gemini', kind: 'platform_api', configured: false }),
      entry({ id: 'openrouter', label: 'OpenRouter', kind: 'platform_api', configured: true, models: [] }),
    ]);
    expect(providerIsUsable(catalog.providers[1])).toBe(true);
    expect(defaultActionModel(catalog)).toBe('openrouter/qwen/qwen3-8b');
  });

  it('openrouter unconfigured -> null, same as any other unconfigured provider', () => {
    const catalog = catalogOf([
      entry({ id: 'gemini', label: 'Gemini', kind: 'platform_api', configured: false }),
      entry({ id: 'openrouter', label: 'OpenRouter', kind: 'platform_api', configured: false, models: [] }),
    ]);
    expect(defaultActionModel(catalog)).toBeNull();
  });
});
