// @vitest-environment jsdom
//
// The picker uses the native top-layer Popover API and preserves outside/inside
// click, single trigger, Escape, and focus-return behavior. It also covers
// provider grouping/search, Ollama reachability, and the "Configure AI
// providers" flow without ever rendering a saved raw key.
//
// Paint-order hit testing remains browser-only because jsdom has no
// layout/paint engine -- every element's getBoundingClientRect is zeroed and
// elementFromPoint always returns null, so that assertion is fundamentally a
// real-browser-only check, not a gap this file's DOM polyfills can honestly
// backfill. The mechanism it depends on (real showPopover()/`:popover-open`
// top-layer membership) IS covered below.

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, waitFor, within, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi, type Mock } from 'vitest';

import { ModelPicker } from '../../src/components/ModelPicker';
import type { LocalHttpEndpointEntry, LocalProviderCatalog, LocalProviderEntry, LocalProviderModel } from '../../src/api/types';
import { installPopoverPolyfill } from '../support/domPolyfills';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    listProviders: vi.fn(),
    providerStatus: vi.fn(),
  };
});

// The configure entry navigates to the settings page; ProviderConfigPanel owns
// the configuration-pane tests.
vi.mock('../../src/routes', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/routes')>();
  return { ...actual, navigate: vi.fn() };
});

import { listProviders, providerStatus } from '../../src/api/open';
import { navigate } from '../../src/routes';



beforeAll(() => {
  installPopoverPolyfill();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function model(id: string, label: string): LocalProviderModel {
  return { id, label, price: null };
}

function providerEntry(overrides: Partial<LocalProviderEntry> & Pick<LocalProviderEntry, 'id' | 'label' | 'kind' | 'models'>): LocalProviderEntry {
  return { configured: false, source: null, hint: null, ...overrides };
}

function localEndpoint(overrides: Partial<LocalHttpEndpointEntry> = {}): LocalHttpEndpointEntry {
  return {
    endpoint_id: 'local-a1b2c3d4e5f6',
    label: 'Ollama',
    kind: 'local_http',
    read_only: false,
    models: [model('ollama/@local-a1b2c3d4e5f6/llama3', 'Llama 3')],
    reachable: true,
    origin: 'http://localhost:11434',
    authority: 'instance',
    source: 'stored',
    detail: null,
    installed_models: ['llama3'],
    protocol: 'ollama_native',
    auth_status: 'ok',
    token_configured: false,
    provisioning_token_configured: false,
    edge_auth: false,
    pull_enabled: true,
    ...overrides,
  };
}

function catalogFixture(): LocalProviderCatalog {
  return {
    schemaVersion: 'frisket.providers.v1',
    tier: 'local',
    providers: [
      // Baseline: every provider usable (keyed / reachable-with-models) --
      // the "everything is set up, browse across providers" case. Tests for
      // the unusable-section affordance (model-picker-provider-affordance-v1)
      // override `configured`/`reachable` per provider on top of this.
      providerEntry({
        id: 'anthropic',
        label: 'Anthropic',
        kind: 'platform_api',
        configured: true,
        models: [model('anthropic/claude-haiku-4-5', 'Claude Haiku 4.5 — fast/cheap')],
      }),
      providerEntry({
        id: 'openai',
        label: 'OpenAI',
        kind: 'platform_api',
        configured: true,
        models: [model('openai/gpt-5-mini', 'GPT-5 mini — fast/cheap')],
      }),
      providerEntry({
        id: 'gemini',
        label: 'Gemini',
        kind: 'platform_api',
        configured: true,
        models: [model('gemini/gemini-2-5-flash', 'Gemini 2.5 Flash — fast/cheap (default)')],
      }),
      localEndpoint(),
    ],
  };
}

async function openPicker(value = 'anthropic/claude-haiku-4-5') {
  const onChange = vi.fn();
  (listProviders as unknown as Mock).mockResolvedValue(catalogFixture());
  render(
    <>
      <span id="test-model-label">Model</span>
      <ModelPicker value={value} onChange={onChange} ariaLabelledBy="test-model-label" />
    </>,
  );
  const button = await screen.findByTestId('model-picker-button');
  await userEvent.click(button);
  const menu = await screen.findByTestId('model-picker-menu');
  return { onChange, button, menu };
}

describe('no provider configured (model-picker-provider-default bugfix)', () => {
  it('shows "No provider configured" instead of presenting the preselected model as ready', async () => {
    const cat = catalogFixture();
    // Nothing configured/reachable anywhere in the catalog.
    cat.providers = cat.providers.map((p) => ({ ...p, configured: false, reachable: false }));
    (listProviders as unknown as Mock).mockResolvedValue(cat);
    render(
      <>
        <span id="test-model-label">Model</span>
        <ModelPicker
          value="gemini/gemini-2-5-flash"
          onChange={vi.fn()}
          ariaLabelledBy="test-model-label"
        />
      </>,
    );

    const notice = await screen.findByTestId('model-picker-no-provider');
    expect(notice).toHaveTextContent(/no provider configured/i);
    expect(screen.queryByText('Gemini 2.5 Flash')).not.toBeInTheDocument();
    expect(screen.getByTestId('model-picker-button')).toHaveClass('is-unconfigured');
  });

  it('a configured provider clears the no-provider state and shows the selected model normally', async () => {
    const { button } = await openPicker('anthropic/claude-haiku-4-5');
    expect(screen.queryByTestId('model-picker-no-provider')).not.toBeInTheDocument();
    expect(button).not.toHaveClass('is-unconfigured');
    expect(within(button).getByText(/haiku/i)).toBeInTheDocument();
  });
});

describe('local-server guidance', () => {
  it('a reachable-but-empty native server shows pull guidance + Recheck, not a bare empty state', async () => {
    const cat = catalogFixture();
    cat.providers = cat.providers.map((p) =>
      p.kind === 'local_http'
        ? {
            ...p,
            reachable: true,
            protocol: 'ollama_native' as const,
            auth_status: 'ok' as const,
            installed_models: [],
            models: [],
          }
        : p,
    );
    (listProviders as unknown as Mock).mockResolvedValue(cat);
    render(
      <>
        <span id="test-model-label">Model</span>
        <ModelPicker
          value="anthropic/claude-haiku-4-5"
          onChange={vi.fn()}
          ariaLabelledBy="test-model-label"
        />
      </>,
    );
    await userEvent.click(await screen.findByTestId('model-picker-button'));
    await userEvent.click(await screen.findByTestId('model-provider-group-local-a1b2c3d4e5f6'));

    const guidance = await screen.findByTestId('local-server-guidance');
    expect(guidance).toHaveTextContent(/ollama pull/);
    expect(screen.queryByText('No models available.')).not.toBeInTheDocument();

    (providerStatus as unknown as Mock).mockResolvedValue(
      cat.providers.find((provider) => provider.kind === 'local_http'),
    );
    await userEvent.click(screen.getByTestId('guidance-recheck'));
    await waitFor(() => expect(providerStatus).toHaveBeenCalledWith('local-a1b2c3d4e5f6'));
    expect(listProviders).toHaveBeenCalledTimes(1);
  });
});

describe('ModelPicker popover mechanism', () => {
  it('is a native top-layer popover (popover=manual, :popover-open, role=dialog retired) with the aria contract intact', async () => {
    const { button, menu } = await openPicker();

    expect(menu).toHaveAttribute('popover', 'manual');
    expect(menu).not.toHaveAttribute('role', 'dialog');
    expect(button).toHaveAttribute('aria-haspopup', 'listbox');
    expect(button).toHaveAttribute('aria-expanded', 'true');
    expect(menu.matches(':popover-open')).toBe(true);
  });

  it('closes on outside pointerdown; a pointerdown inside the popover does not', async () => {
    const { menu } = await openPicker();
    const search = screen.getByTestId('model-picker-search');

    fireEvent.pointerDown(search);
    expect(menu).toBeInTheDocument();

    fireEvent.pointerDown(document.body);
    await waitFor(() => expect(screen.queryByTestId('model-picker-menu')).not.toBeInTheDocument());
  });

  it('the trigger toggles exactly once per click -- second click closes, no reopen', async () => {
    const { button } = await openPicker();
    expect(screen.getByTestId('model-picker-menu')).toBeInTheDocument();

    await userEvent.click(button);
    expect(screen.queryByTestId('model-picker-menu')).not.toBeInTheDocument();
  });

  it('Escape in the search input does not dismiss the menu (escape:false, structural via popover=manual)', async () => {
    const { menu } = await openPicker();
    const search = screen.getByTestId('model-picker-search') as HTMLInputElement;

    await userEvent.type(search, 'haiku');
    fireEvent.keyDown(search, { key: 'Escape' });

    // No Escape listener exists on this surface at all (popover="manual" grants
    // no native light-dismiss either) -- the menu survives untouched. The
    // browser's own `input[type=search]` Escape-clears-value UA behavior is not
    // asserted here: jsdom does not implement it, and it isn't this
    // component's code to test.
    expect(menu).toBeInTheDocument();
    expect(search).toHaveValue('haiku');
  });

  it('focus returns to the trigger on close when the search input was last-focused', async () => {
    const { button, onChange } = await openPicker();
    const search = screen.getByTestId('model-picker-search');
    search.focus();
    expect(search).toHaveFocus();

    await userEvent.click(screen.getByTestId('model-option-anthropic-claude-haiku-4-5'));
    expect(onChange).toHaveBeenCalledWith('anthropic/claude-haiku-4-5');
    expect(screen.queryByTestId('model-picker-menu')).not.toBeInTheDocument();

    await act(async () => {
      await new Promise((resolve) => requestAnimationFrame(resolve));
    });
    expect(button).toHaveFocus();
  });
});

describe('ModelPicker provider grouping, search, and configure-providers', () => {
  it('keeps models attributed to their local server and returns the qualified model id', async () => {
    const cat = catalogFixture();
    cat.providers.push(
      localEndpoint({
        endpoint_id: 'local-b1c2d3e4f5a6',
        label: 'LM Studio — gaming PC',
        origin: 'http://localhost:1234',
        models: [model('ollama/@local-b1c2d3e4f5a6/qwen', 'Qwen')],
      }),
    );
    (listProviders as unknown as Mock).mockResolvedValue(cat);
    const onChange = vi.fn();
    render(
      <>
        <span id="test-model-label">Model</span>
        <ModelPicker
          value="ollama/@local-a1b2c3d4e5f6/llama3"
          onChange={onChange}
          ariaLabelledBy="test-model-label"
        />
      </>,
    );

    await userEvent.click(await screen.findByTestId('model-picker-button'));
    const extraGroup = screen.getByTestId(
      'model-provider-group-local-b1c2d3e4f5a6',
    );
    expect(extraGroup).toHaveTextContent('LM Studio — gaming PC');
    await userEvent.click(extraGroup);
    await userEvent.click(
      screen.getByTestId('model-option-ollama-local-b1c2d3e4f5a6-qwen'),
    );

    expect(onChange).toHaveBeenCalledWith('ollama/@local-b1c2d3e4f5a6/qwen');
  });

  it('groups models by provider, reveals a hovered/focused provider\'s models, shows the Ollama reachability badge, and search narrows + selects', async () => {
    const { onChange, button } = await openPicker();

    for (const provider of ['anthropic', 'openai', 'gemini', 'local-a1b2c3d4e5f6']) {
      expect(screen.getByTestId(`model-provider-group-${provider}`)).toBeInTheDocument();
    }

    await userEvent.hover(screen.getByTestId('model-provider-group-openai'));
    expect(screen.getByTestId('model-option-openai-gpt-5-mini')).toBeInTheDocument();

    fireEvent.focus(screen.getByTestId('model-provider-group-gemini'));
    expect(screen.getByTestId('model-option-gemini-gemini-2-5-flash')).toBeInTheDocument();

    const badge = screen.getByTestId('local-server-reachability-local-a1b2c3d4e5f6');
    expect(badge).toBeInTheDocument();
    expect(badge.textContent).toMatch(/reachable|offline/i);

    const search = screen.getByTestId('model-picker-search');
    await userEvent.type(search, 'haiku');
    const haiku = screen.getByTestId('model-option-anthropic-claude-haiku-4-5');
    expect(haiku).toBeInTheDocument();
    await userEvent.click(haiku);

    expect(onChange).toHaveBeenCalledWith('anthropic/claude-haiku-4-5');
    expect(screen.queryByTestId('model-picker-menu')).not.toBeInTheDocument();
    expect(within(button).getByText(/haiku/i)).toBeInTheDocument();
  });

  it('Configure AI providers… navigates to the workspace settings section and closes the menu', async () => {
    await openPicker();

    await userEvent.click(screen.getByTestId('configure-providers'));

    expect(navigate).toHaveBeenCalledWith({
      kind: 'settings',
      scope: 'personal',
      section: 'ai-providers',
    });
    await waitFor(() =>
      expect(screen.queryByTestId('model-picker-menu')).not.toBeInTheDocument(),
    );
  });
});

// Maintainer spec, 2026-07-31 (verbatim): "I think we always show all models
// even if the user doesn't have API keys in for them. If you don't have an
// anthropic api key, going to the anthropic section should just give you a
// 'Add Anthropic API key...'." Every provider section still shows up
// (catalogFixture's four groups are still all present in every case below) --
// only whether a section LISTS ITS MODELS changes, gated by the shared
// providerIsUsable (actions/model.ts) so this can't drift from
// defaultActionModel's own notion of "usable".
describe('unusable-provider affordance (model-picker-provider-affordance-v1)', () => {
  it('an unusable provider does not list its models and shows "Add OpenAI API key…" instead -- the already-selected model from that provider is untouched', async () => {
    const cat = catalogFixture();
    cat.providers = cat.providers.map((p) => (p.id === 'openai' ? { ...p, configured: false } : p));
    (listProviders as unknown as Mock).mockResolvedValue(cat);
    const onChange = vi.fn();
    render(
      <>
        <span id="test-model-label">Model</span>
        {/* value already points at a model in the now-unusable provider --
            selection correction is only ActionForm's fresh-launch effect,
            never this component, so it must render exactly as selected. */}
        <ModelPicker value="openai/gpt-5-mini" onChange={onChange} ariaLabelledBy="test-model-label" />
      </>,
    );
    const button = await screen.findByTestId('model-picker-button');
    expect(within(button).getByText(/gpt-5 mini/i)).toBeInTheDocument();
    expect(screen.queryByTestId('model-picker-no-provider')).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();

    await userEvent.click(button);
    // The section is still listed -- nothing is hidden from the left rail.
    expect(screen.getByTestId('model-provider-group-openai')).toBeInTheDocument();
    await userEvent.click(screen.getByTestId('model-provider-group-openai'));

    expect(screen.queryByTestId('model-option-openai-gpt-5-mini')).not.toBeInTheDocument();
    const affordance = screen.getByTestId('model-picker-add-key-openai');
    expect(affordance).toHaveTextContent(/add openai api key/i);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('a usable provider still lists its models, with no affordance row', async () => {
    await openPicker();
    await userEvent.click(screen.getByTestId('model-provider-group-anthropic'));

    expect(screen.getByTestId('model-option-anthropic-claude-haiku-4-5')).toBeInTheDocument();
    expect(screen.queryByTestId('model-picker-add-key-anthropic')).not.toBeInTheDocument();
  });

  it('the affordance navigates to the AI providers settings surface and closes the menu, same destination as "Configure AI providers…"', async () => {
    const cat = catalogFixture();
    cat.providers = cat.providers.map((p) => (p.id === 'openai' ? { ...p, configured: false } : p));
    (listProviders as unknown as Mock).mockResolvedValue(cat);
    render(
      <>
        <span id="test-model-label">Model</span>
        <ModelPicker value="anthropic/claude-haiku-4-5" onChange={vi.fn()} ariaLabelledBy="test-model-label" />
      </>,
    );
    await userEvent.click(await screen.findByTestId('model-picker-button'));
    await userEvent.click(await screen.findByTestId('model-provider-group-openai'));
    await userEvent.click(screen.getByTestId('model-picker-add-key-openai'));

    expect(navigate).toHaveBeenCalledWith({
      kind: 'settings',
      scope: 'personal',
      section: 'ai-providers',
    });
    await waitFor(() =>
      expect(screen.queryByTestId('model-picker-menu')).not.toBeInTheDocument(),
    );
  });

  it('ollama unusable with no probe facts to explain why falls back to the same honest affordance, not the rich install-guidance panel', async () => {
    const cat = catalogFixture();
    cat.providers = cat.providers.map((p) =>
      p.kind === 'local_http' ? { ...p, reachable: true, models: [] } : p,
    );
    (listProviders as unknown as Mock).mockResolvedValue(cat);
    render(
      <>
        <span id="test-model-label">Model</span>
        <ModelPicker value="anthropic/claude-haiku-4-5" onChange={vi.fn()} ariaLabelledBy="test-model-label" />
      </>,
    );
    await userEvent.click(await screen.findByTestId('model-picker-button'));
    await userEvent.click(await screen.findByTestId('model-provider-group-local-a1b2c3d4e5f6'));

    expect(screen.queryByTestId('local-server-guidance')).not.toBeInTheDocument();
    expect(screen.getByTestId('model-picker-add-key-local-a1b2c3d4e5f6')).toHaveTextContent(/check ollama/i);
  });

  // The live catalog remains authoritative when available. These mocks pin
  // selectable, configured-empty, and unconfigured states independently of
  // the curated offline fallback.
  describe('openrouter live catalog section', () => {
    function catalogWithOpenrouter(openrouter: Partial<LocalProviderEntry>): LocalProviderCatalog {
      const cat = catalogFixture();
      cat.providers = [
        ...cat.providers,
        providerEntry({
          id: 'openrouter',
          label: 'OpenRouter',
          kind: 'platform_api',
          configured: false,
          models: [],
          ...openrouter,
        }),
      ];
      return cat;
    }

    it('configured with live models -> models are listed and selectable', async () => {
      const cat = catalogWithOpenrouter({
        configured: true,
        models: [model('openrouter/anthropic/claude-3.5-sonnet', 'Claude 3.5 Sonnet')],
      });
      (listProviders as unknown as Mock).mockResolvedValue(cat);
      const onChange = vi.fn();
      render(
        <>
          <span id="test-model-label">Model</span>
          <ModelPicker value="anthropic/claude-haiku-4-5" onChange={onChange} ariaLabelledBy="test-model-label" />
        </>,
      );
      await userEvent.click(await screen.findByTestId('model-picker-button'));
      await userEvent.click(await screen.findByTestId('model-provider-group-openrouter'));

      const option = screen.getByTestId('model-option-openrouter-anthropic-claude-3-5-sonnet');
      expect(option).toBeInTheDocument();
      expect(screen.queryByTestId('model-picker-add-key-openrouter')).not.toBeInTheDocument();
      await userEvent.click(option);
      expect(onChange).toHaveBeenCalledWith('openrouter/anthropic/claude-3.5-sonnet');
    });

    it('configured but the live catalog lists no models -> honest empty state, no affordance (nothing to add)', async () => {
      const cat = catalogWithOpenrouter({ configured: true, models: [] });
      (listProviders as unknown as Mock).mockResolvedValue(cat);
      render(
        <>
          <span id="test-model-label">Model</span>
          <ModelPicker value="anthropic/claude-haiku-4-5" onChange={vi.fn()} ariaLabelledBy="test-model-label" />
        </>,
      );
      await userEvent.click(await screen.findByTestId('model-picker-button'));
      await userEvent.click(await screen.findByTestId('model-provider-group-openrouter'));

      expect(screen.getByText('No models available.')).toBeInTheDocument();
      expect(screen.queryByTestId('model-picker-add-key-openrouter')).not.toBeInTheDocument();
    });

    it('unconfigured -> the standard add-key affordance', async () => {
      const cat = catalogWithOpenrouter({ configured: false, models: [] });
      (listProviders as unknown as Mock).mockResolvedValue(cat);
      render(
        <>
          <span id="test-model-label">Model</span>
          <ModelPicker value="anthropic/claude-haiku-4-5" onChange={vi.fn()} ariaLabelledBy="test-model-label" />
        </>,
      );
      await userEvent.click(await screen.findByTestId('model-picker-button'));
      await userEvent.click(await screen.findByTestId('model-provider-group-openrouter'));

      expect(screen.getByTestId('model-picker-add-key-openrouter')).toHaveTextContent(/add openrouter api key/i);
    });
  });

  it('all-providers-unusable coexistence: the trigger still reads "No provider configured" and every section shows its own add-key row', async () => {
    const cat = catalogFixture();
    cat.providers = cat.providers.map((p) => ({ ...p, configured: false, reachable: false }));
    (listProviders as unknown as Mock).mockResolvedValue(cat);
    render(
      <>
        <span id="test-model-label">Model</span>
        <ModelPicker value="gemini/gemini-2-5-flash" onChange={vi.fn()} ariaLabelledBy="test-model-label" />
      </>,
    );

    expect(await screen.findByTestId('model-picker-no-provider')).toBeInTheDocument();
    expect(screen.getByTestId('model-picker-button')).toHaveClass('is-unconfigured');

    await userEvent.click(screen.getByTestId('model-picker-button'));
    const modelTestId: Record<string, string> = {
      anthropic: 'model-option-anthropic-claude-haiku-4-5',
      openai: 'model-option-openai-gpt-5-mini',
      gemini: 'model-option-gemini-gemini-2-5-flash',
    };
    for (const provider of ['anthropic', 'openai', 'gemini']) {
      await userEvent.click(screen.getByTestId(`model-provider-group-${provider}`));
      expect(screen.getByTestId(`model-picker-add-key-${provider}`)).toBeInTheDocument();
      expect(screen.queryByTestId(modelTestId[provider])).not.toBeInTheDocument();
    }
  });
});
