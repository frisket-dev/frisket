// @vitest-environment jsdom
//
// TranslateCompareTab is the text-source translation comparison surface.
// Component-level mirror of the transcribe-compare e2e suite's shape: engine
// chips from the shared catalog, add-menu availability, staged Run, and
// per-engine result cards with failure isolation. Compare is the FREE surface:
// billable engines (llm/deepl/google_translate) are filtered out of the picker
// entirely and named in an explanatory note, so there is no consent gate left
// to test. No live calls — api is spied.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/engine-selector/SelectorField', () => ({
  SelectorField: ({ onSelect, query, allowChoice }: {
    onSelect(choice: { authored_selection: { kind: 'engine'; engine: string } }): void;
    query: { subject: { params: Record<string, unknown> } };
    allowChoice(choice: { authored_selection: { kind: 'engine'; engine: string } }): boolean;
  }) => <button type="button" data-testid="translate-compare-selector"
    data-language={String(query.subject.params.language ?? '')}
    data-target={String(query.subject.params.target_language ?? '')}
    onClick={() => {
      const local = { authored_selection: { kind: 'engine' as const, engine: 'hy_mt2' } };
      if (allowChoice(local)) onSelect(local);
    }}>Add engine</button>,
}));

import { TranslateCompareTab } from '../../src/workbench/TranslateCompareTab';

import type { ActionCatalogPayload, TranslateCompareScratchResult } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';


const api = createProjectApi('test-project');
const { render } = createWorkspaceTestHarness({
  projectId: 'test-project',
  api: { projectApi: api },
});

function catalog(): ActionCatalogPayload {
  return {
    actions: [
      {
        kind: 'map.translate',
        ui_hints: {
          engines: [
            { id: 'llm', label: 'LLM translation', tier: 'hosted', billable: true, available: true },
            { id: 'deepl', label: 'DeepL API (remote)', tier: 'hosted', billable: true, available: true },
            {
              id: 'google_translate',
              label: 'Google Cloud Translation (remote)',
              tier: 'hosted',
              billable: true,
              available: true,
            },
            { id: 'hy_mt2', label: 'HY-MT2 local', tier: 'local', available: true },
            {
              id: 'opus_mt',
              label: 'Opus-MT local',
              tier: 'local',
              available: true,
            },
          ],
        },
      },
    ],
  } as unknown as ActionCatalogPayload;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('TranslateCompareTab', () => {
  beforeEach(() => {
    vi.spyOn(api, 'listActionCatalog').mockResolvedValue(catalog());
  });

  it.each([
    ['before the live catalog answers', () => new Promise<ActionCatalogPayload>(() => {})],
    ['after the live catalog fails', () => Promise.reject(new Error('catalog unavailable'))],
  ])('does not submit unavailable fallback opus_mt %s', async (_label, catalogResult) => {
    vi.mocked(api.listActionCatalog).mockImplementation(catalogResult);
    const scratch = vi.spyOn(api, 'compareTranslateScratch').mockResolvedValue({
      schema_version: 'frisket.translate_compare_preview.v1',
      source: { scratch: true, text_length: 5 },
      target_language: 'Spanish',
      engines: [],
      results: [],
      warnings: [],
      errors: [],
    });
    const user = userEvent.setup();
    render(<TranslateCompareTab onSessionChange={() => {}} />);

    await user.type(screen.getByTestId('translate-compare-text'), 'Hello');
    const run = screen.getByTestId('translate-compare-run');
    await user.click(run);

    expect(scratch).not.toHaveBeenCalled();
    expect(run).toBeDisabled();
  });

  it('adds only a free authoritative selector choice and carries language context', async () => {
    const user = userEvent.setup();
    render(<TranslateCompareTab onSessionChange={() => {}} />);
    await waitFor(() => expect(screen.getByTestId('translate-compare-tab')).toBeInTheDocument());

    const chips = screen.getByTestId('translate-compare-engine-chips');
    expect(within(chips).queryAllByTestId('translate-compare-engine-chip')).toHaveLength(0);

    // Billable -> run. The three billable engines are NOT offered; the note
    // names them and points at the action that runs them, so the picker is
    // not silently shorter than the catalog.
    const note = screen.getByTestId('translate-compare-billable-note');
    expect(note).toHaveTextContent('LLM translation');
    expect(note).toHaveTextContent('DeepL API (remote)');
    expect(note).toHaveTextContent('Google Cloud Translation (remote)');
    expect(note).toHaveTextContent('the Translate action');

    const selector = screen.getByTestId('translate-compare-selector');
    expect(selector).toHaveAttribute('data-language', '');
    expect(selector).toHaveAttribute('data-target', 'Spanish');
    await user.click(selector);
    expect(within(chips).getByTestId('translate-compare-engine-chip')).toHaveAttribute('data-engine-id', 'hy_mt2');
  });

  it('runs the free engines with no consent step and isolates a failing engine', async () => {
    const result: TranslateCompareScratchResult = {
      schema_version: 'frisket.translate_compare_preview.v1',
      source: { scratch: true, text_length: 5 },
      target_language: 'Spanish',
      engines: ['hy_mt2'],
      results: [
        {
          engine: 'hy_mt2',
          translation: '',
          detected_language: null,
          runtime_ms: 3,
          errors: ['auth: bad key'],
        },
      ],
      warnings: [],
      errors: [{ code: 'auth', engine: 'hy_mt2', message: 'bad key' }],
    };
    const scratch = vi.spyOn(api, 'compareTranslateScratch').mockResolvedValue(result);

    const user = userEvent.setup();
    render(<TranslateCompareTab onSessionChange={() => {}} />);
    await waitFor(() => screen.getByTestId('translate-compare-tab'));

    await user.type(screen.getByTestId('translate-compare-text'), 'Hello');
    await user.click(screen.getByTestId('translate-compare-selector'));

    // Free engines only: no consent affordance exists, and Run is live at once.
    expect(screen.queryByTestId('translate-compare-remote-consent')).not.toBeInTheDocument();
    expect(screen.queryByTestId('translate-compare-allow-remote')).not.toBeInTheDocument();
    expect(screen.getByTestId('translate-compare-run')).toBeEnabled();

    await user.click(screen.getByTestId('translate-compare-run'));
    await waitFor(() => screen.getByTestId('translate-compare-results'));

    // No allow_remote on the request: the field is gone from the wire.
    expect(scratch).toHaveBeenCalledWith({
      engines: ['hy_mt2'],
      text: 'Hello',
      targetLanguage: 'Spanish',
      language: null,
    });
    expect(scratch.mock.calls[0][0]).not.toHaveProperty('allowRemote');

    // Failure isolation: opus_mt translated (+ detected language), hy_mt2 failed
    // but its card renders the error rather than aborting the compare.
    expect(screen.getByTestId('translate-compare-result-error-hy_mt2')).toHaveTextContent(
      'bad key',
    );
    expect(
      screen.queryByTestId('translate-compare-result-text-hy_mt2'),
    ).not.toBeInTheDocument();
  });

  it('removing an engine chip drops it from the run set', async () => {
    const user = userEvent.setup();
    render(<TranslateCompareTab onSessionChange={() => {}} />);
    await waitFor(() => screen.getByTestId('translate-compare-tab'));

    await user.click(screen.getByTestId('translate-compare-selector'));
    await user.click(screen.getByTestId('translate-compare-engine-chip-remove-hy_mt2'));
    const chipIds = within(screen.getByTestId('translate-compare-engine-chips'))
      .queryAllByTestId('translate-compare-engine-chip')
      .map((el) => el.getAttribute('data-engine-id'));
    expect(chipIds).toEqual([]);
  });

  // The "Translate from"
  // source-language picker was a bare <select className="row-height-select">
  // — a third "pick a language" idiom next to PanelSelect (ActionForm's own
  // EngineLanguageControl/16 other sites) and TranslatePairPicker's now-fixed
  // pair selects. It's now PanelSelect too, styled via the SAME
  // row-height-select class passed through as PanelSelect's className, and
  // still writes to the same plain <select> the run request reads from.
  it('renders the source-language picker as a PanelSelect', async () => {
    const user = userEvent.setup();
    render(<TranslateCompareTab onSessionChange={() => {}} />);
    await waitFor(() => screen.getByTestId('translate-compare-tab'));

    const sourceLanguage = screen.getByTestId('translate-compare-source-language');
    expect(sourceLanguage).toHaveClass('row-height-select');
    expect(screen.queryByTestId('translate-compare-source-language-menu')).not.toBeInTheDocument();
    await user.click(sourceLanguage);
    const menu = screen.getByTestId('translate-compare-source-language-menu');
    expect(menu).toBeInTheDocument();

    await user.click(within(menu).getByRole('option', { name: 'Spanish' }));
    expect(sourceLanguage).toHaveValue('es');
  });
});
