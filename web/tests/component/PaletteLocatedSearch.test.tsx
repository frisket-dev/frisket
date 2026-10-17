// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WorkbenchCommandPalette } from '../../src/workbench/WorkbenchCommandPalette';
import { ModelSetupRequestError } from '../../src/api/engineSetup';

const searchProject = vi.fn();
const startEngineSetup = vi.fn();
const listModelPulls = vi.fn();
const getModelPull = vi.fn();
const orgListModelPulls = vi.fn();
const orgGetModelPull = vi.fn();
const createSearchPreparation = vi.fn();
const listSearchPreparations = vi.fn();
const getSearchPreparation = vi.fn();
const decideSearchPreparation = vi.fn();
const getSearchPreparationTextSamples = vi.fn();
const embeddingIndexes = vi.fn();
const saveLens = vi.fn();
const createWatch = vi.fn();
const projectApi = { embeddingIndexes, saveLens, createWatch };

vi.mock('../../src/api/open', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/open')>(),
  searchProject: (...args: unknown[]) => searchProject(...args),
  listModelPulls: (...args: unknown[]) => listModelPulls(...args),
  getModelPull: (...args: unknown[]) => getModelPull(...args),
  orgListModelPulls: (...args: unknown[]) => orgListModelPulls(...args),
  orgGetModelPull: (...args: unknown[]) => orgGetModelPull(...args),
}));

vi.mock('../../src/api/engineSetup', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/engineSetup')>(),
  startEngineSetup: (...args: unknown[]) => startEngineSetup(...args),
}));

vi.mock('../../src/api/searchPreparation', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/searchPreparation')>(),
  createSearchPreparation: (...args: unknown[]) => createSearchPreparation(...args),
  listSearchPreparations: (...args: unknown[]) => listSearchPreparations(...args),
  getSearchPreparation: (...args: unknown[]) => getSearchPreparation(...args),
  decideSearchPreparation: (...args: unknown[]) => decideSearchPreparation(...args),
  getSearchPreparationTextSamples: (...args: unknown[]) => getSearchPreparationTextSamples(...args),
}));

vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({
    chromePreferences: { projectId: 'project-1' },
    projectApi,
  }),
}));

const location = {
  kind: 'text_chunk',
  precision: 'exact',
  text: { text_surface_id: 'cell:2:3', content_hash: 'hash', start: 0, end: 20 },
  pages: null,
  time: null,
  source_span_ids: [],
} as const;

function hit(partId: string, snippet: string) {
  return {
    source: {
      source_id: 'cell:2:3',
      source_kind: 'text',
      version_kind: 'retrieval_revision',
      version_value: 1,
      content_hash: 'hash',
    },
    part: { part_id: partId, part_kind: 'text_chunk', producer_recipe_id: 'text-v1' },
    binding: {
      binding_id: 'binding:2:3',
      sheet_id: 1,
      row_id: 2,
      column_id: 3,
      dependencies: [{ row_id: 2, column_id: 3, retrieval_revision: 1 }],
    },
    location,
    column_name: 'body',
    score: 0.8,
    rerank_score: null,
    contributions: [{
      method: 'semantic',
      channel: 'text',
      score: 0.8,
      rank: 1,
      representation_id: 'representation:1',
      space_id: 'space:1',
    }],
    snippet,
    ai_generated: false,
    additional_binding_count: 0,
    navigation: { kind: 'cell', sheet_id: 1, row_id: 2, column_id: 3, text_range: null },
  } as const;
}

const completeResponse = {
  groups: [{
    group_key: 'source:1',
    primary: hit('part:1', '<b>budget</b> approved'),
    supporting: [hit('part:2', 'supporting passage')],
    additional_count: 2,
  }],
  coverage: {
    requested_mode: 'hybrid',
    effective_modes: ['lexical', 'semantic'],
    state: 'complete',
    searched_channels: ['text'],
    channels: [],
    is_top_k: true,
    truncated: false,
  },
} as const;

function setupPull(overrides: Record<string, unknown> = {}) {
  return {
    schemaVersion: 'frisket.model_pull.v4',
    id: 71,
    operation_kind: 'engine_setup',
    display_name: 'EmbeddingGemma 2',
    model: 'engine-setup:embeddinggemma-2.local@1',
    status: 'running',
    capabilities: { cancel: true, retry: false, remove: false },
    endpoint_id: null,
    endpoint_origin: null,
    cancel_requested: false,
    error: null,
    created_at: '2026-10-16T00:00:00Z',
    started_at: '2026-10-16T00:00:01Z',
    finished_at: null,
    initiated_by: null,
    phase: 'Downloading model',
    completed_bytes: 100,
    total_bytes: 1_000,
    resolved_digest: null,
    resolved_size: null,
    artifact: null,
    ...overrides,
  };
}

function PaletteHarness({
  searchSheets = [{ id: '1', name: 'Documents' } as never],
  activeSearchSheetId = '1',
  onClose = vi.fn(),
}: {
  searchSheets?: never[];
  activeSearchSheetId?: string;
  onClose?: () => void;
} = {}) {
  const [query, setQuery] = useState('budget');
  return (
    <WorkbenchCommandPalette
      onClose={onClose}
      commands={[]}
      onHideContribution={vi.fn()}
      onRevealContribution={vi.fn()}
      lastAction=""
      projectId="project-1"
      visibilityTargets={[]}
      query={query}
      onQueryChange={setQuery}
      bestMatchItems={[]}
      actionItems={[]}
      gotoItems={[]}
      onLaunchAction={vi.fn()}
      onNavigateSheet={vi.fn()}
      searchSheets={searchSheets}
      activeSearchSheetId={activeSearchSheetId}
      onNavigateSearchHit={vi.fn()}
    />
  );
}

function AnchoredPaletteHarness() {
  const [query, setQuery] = useState('');
  return (
    <WorkbenchCommandPalette
      onClose={vi.fn()}
      commands={[]}
      onHideContribution={vi.fn()}
      onRevealContribution={vi.fn()}
      lastAction=""
      visibilityTargets={[]}
      query={query}
      onQueryChange={setQuery}
      bestMatchItems={[{ actionKind: 'map.extract', name: 'Extract fields' }]}
      actionItems={[]}
      gotoItems={[]}
      onLaunchAction={vi.fn()}
      onNavigateSheet={vi.fn()}
      searchSheets={[{ id: '1', name: 'Documents' } as never]}
      activeSearchSheetId="1"
      searchSeed={{
        namedIndexId: 'index-1',
        label: 'page 2',
        anchor: {
          kind: 'cell',
          sheet_id: 1,
          row_id: 2,
          column_id: 3,
          exclude_exact: true,
          location: {
            kind: 'document_page',
            precision: 'exact',
            text: null,
            pages: { start: 2, end: 2 },
            time: null,
            source_span_ids: [],
          },
        },
      }}
      onNavigateSearchHit={vi.fn()}
    />
  );
}

beforeEach(() => {
  searchProject.mockReset();
  startEngineSetup.mockReset();
  listModelPulls.mockReset();
  getModelPull.mockReset();
  orgListModelPulls.mockReset();
  orgGetModelPull.mockReset();
  createSearchPreparation.mockReset();
  listSearchPreparations.mockReset();
  getSearchPreparation.mockReset();
  decideSearchPreparation.mockReset();
  getSearchPreparationTextSamples.mockReset();
  getSearchPreparationTextSamples.mockResolvedValue({ samples: [] });
  embeddingIndexes.mockReset();
  saveLens.mockReset();
  createWatch.mockReset();
  embeddingIndexes.mockResolvedValue([{
    indexId: 'index-1',
    name: 'Research passages',
    sheetId: 1,
  }]);
  saveLens.mockResolvedValue({});
  createWatch.mockResolvedValue({});
  listModelPulls.mockResolvedValue({ pulls: [] });
  orgListModelPulls.mockResolvedValue({ pulls: [] });
  listSearchPreparations.mockResolvedValue({ batches: [] });
});

afterEach(cleanup);

describe('located palette search', () => {
  it('puts an explicit similarity anchor ahead of unrelated palette commands', async () => {
    searchProject.mockResolvedValue({
      ...completeResponse,
      response_anchor: {
        kind: 'located',
        source: completeResponse.groups[0].primary.source,
        binding: completeResponse.groups[0].primary.binding,
        location: {
          kind: 'document_page',
          precision: 'exact',
          text: null,
          pages: { start: 3, end: 3 },
          time: null,
          source_span_ids: [],
        },
        exclude_exact: true,
      },
    });
    render(<AnchoredPaletteHarness />);

    expect(await screen.findByTestId('search-anchor')).toHaveTextContent('Similar to page 2');
    expect(screen.queryByText('Extract fields')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('search-anchor')).toHaveTextContent(
      'Similar to page 3',
    ));

    fireEvent.click(screen.getByRole('button', { name: 'Clear similarity anchor' }));
    expect(screen.getByText('Extract fields')).toBeVisible();
  });

  it('renders grouped supporting hits and searches from the selected exact hit', async () => {
    searchProject.mockResolvedValue(completeResponse);
    render(<PaletteHarness />);

    expect(await screen.findByText('supporting passage')).toBeVisible();
    expect(screen.getByText('2 more related results')).toBeVisible();
    expect(screen.queryByTestId('palette-empty-state')).not.toBeInTheDocument();
    fireEvent.click(screen.getAllByTestId('search-hit-find-similar')[0]);

    await waitFor(() => expect(searchProject).toHaveBeenCalledTimes(2));
    expect(searchProject.mock.calls[1][1]).toMatchObject({
      mode: 'semantic',
      query: {
        kind: 'similar',
        anchor: {
          kind: 'located',
          source: completeResponse.groups[0].primary.source,
          location,
          exclude_exact: true,
        },
      },
    });
    expect(screen.getByTestId('search-anchor')).toHaveTextContent('budget approved');
  });

  it.each(['unavailable', 'partial'] as const)('discloses semantic setup without claiming work started (%s)', async (state) => {
    searchProject.mockResolvedValue({
      groups: [],
      coverage: {
        requested_mode: 'hybrid',
        effective_modes: ['lexical'],
        state,
        searched_channels: ['text'],
        channels: [{
          channel: 'semantic',
          state: 'unavailable',
          reason: 'model unavailable',
          recipe_id: null,
          setup: {
            setup_ref: 'engine-setup:embeddinggemma-2.local@1',
            scope: 'workspace',
            display_name: 'EmbeddingGemma 2',
            download_bytes: 1_525_784_180,
            additional_runtime_download: true,
            license: 'Apache-2.0',
            source_url: 'https://example.test/embeddinggemma',
          },
        }],
        is_top_k: true,
        truncated: false,
      },
    });
    startEngineSetup.mockResolvedValue({ pull: setupPull(), deduplicated: false });
    getModelPull.mockResolvedValue(setupPull({
      status: 'done',
      phase: null,
      completed_bytes: 1_000,
      resolved_size: 1_000,
      finished_at: '2026-10-16T00:01:00Z',
    }));
    render(<PaletteHarness />);

    fireEvent.click(await screen.findByTestId('search-setup'));
    expect(startEngineSetup).not.toHaveBeenCalled();
    expect(screen.queryByText(/while more material is prepared/i)).not.toBeInTheDocument();
    const consent = screen.getByTestId('search-setup-consent');
    expect(consent).toHaveTextContent('Download EmbeddingGemma 2?');
    expect(consent).toHaveTextContent('About 1.4 GB');
    fireEvent.click(screen.getByTestId('search-setup-confirm'));
    await waitFor(() => expect(startEngineSetup).toHaveBeenCalledWith(
      'workspace',
      'engine-setup:embeddinggemma-2.local@1',
    ));
    expect(await screen.findByTestId('search-setup-status')).toBeVisible();
    expect(screen.queryByTestId('model-pull-progress')).not.toBeInTheDocument();
    expect(searchProject).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(searchProject).toHaveBeenCalledTimes(2), { timeout: 2_500 });
    expect(screen.queryByTestId('search-setup-status')).not.toBeInTheDocument();
    expect(screen.queryByText('Setting up semantic search')).not.toBeInTheDocument();
  });

  it('restores a matching durable semantic setup pull when Search reopens', async () => {
    searchProject.mockResolvedValue({
      groups: [],
      coverage: {
        requested_mode: 'semantic', effective_modes: ['lexical'], state: 'unavailable',
        searched_channels: ['text'], is_top_k: true, truncated: false,
        channels: [{
          channel: 'semantic', state: 'unavailable', reason: 'model unavailable', recipe_id: null,
          setup: {
            setup_ref: 'engine-setup:embeddinggemma-2.local@1', scope: 'workspace',
            display_name: 'EmbeddingGemma 2', download_bytes: 1_525_784_180,
            additional_runtime_download: true, license: 'Apache-2.0',
            source_url: 'https://example.test/embeddinggemma',
          },
        }],
      },
    });
    listModelPulls.mockResolvedValue({ pulls: [setupPull()] });
    getModelPull.mockReturnValue(new Promise(() => {}));

    render(<PaletteHarness />);

    expect(await screen.findByTestId('search-setup-status')).toBeVisible();
    expect(screen.queryByTestId('model-pull-progress')).not.toBeInTheDocument();
    expect(startEngineSetup).not.toHaveBeenCalled();
  });

  it('keeps semantic setup failures visible without treating them as readiness', async () => {
    searchProject.mockResolvedValue({
      groups: [],
      coverage: {
        requested_mode: 'semantic', effective_modes: ['lexical'], state: 'unavailable',
        searched_channels: ['text'], is_top_k: true, truncated: false,
        channels: [{
          channel: 'semantic', state: 'unavailable', reason: 'model unavailable', recipe_id: null,
          setup: {
            setup_ref: 'engine-setup:embeddinggemma-2.local@1', scope: 'workspace',
            display_name: 'EmbeddingGemma 2', download_bytes: 1_525_784_180,
            additional_runtime_download: true, license: 'Apache-2.0',
            source_url: 'https://example.test/embeddinggemma',
          },
        }],
      },
    });
    startEngineSetup
      .mockResolvedValueOnce({ pull: setupPull(), deduplicated: false })
      .mockResolvedValueOnce({ pull: setupPull({ id: 72 }), deduplicated: false });
    getModelPull.mockResolvedValue(setupPull({
      status: 'failed',
      capabilities: { cancel: false, retry: true, remove: false },
      error: { code: 'download_failed', message: 'Could not download model' },
      finished_at: '2026-10-16T00:01:00Z',
    }));

    render(<PaletteHarness />);
    fireEvent.click(await screen.findByTestId('search-setup'));
    fireEvent.click(screen.getByTestId('search-setup-confirm'));

    expect(await screen.findByTestId('search-setup-status')).toBeVisible();
    await waitFor(() => expect(screen.getByTestId('search-setup-error')).toHaveTextContent(
      'Could not download model',
    ), { timeout: 2_500 });
    expect(searchProject).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTestId('search-setup-retry'));
    await waitFor(() => expect(startEngineSetup).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId('model-pull-progress')).not.toBeInTheDocument();
    expect(searchProject).toHaveBeenCalledTimes(1);
  });

  it('does not mistake an unrelated busy pull for semantic setup', async () => {
    searchProject.mockResolvedValue({
      groups: [],
      coverage: {
        requested_mode: 'semantic', effective_modes: ['lexical'], state: 'unavailable',
        searched_channels: ['text'], is_top_k: true, truncated: false,
        channels: [{
          channel: 'semantic', state: 'unavailable', reason: 'model unavailable', recipe_id: null,
          setup: {
            setup_ref: 'engine-setup:embeddinggemma-2.local@1', scope: 'workspace',
            display_name: 'EmbeddingGemma 2', download_bytes: 1_525_784_180,
            additional_runtime_download: true, license: 'Apache-2.0',
            source_url: 'https://example.test/embeddinggemma',
          },
        }],
      },
    });
    startEngineSetup.mockRejectedValue(new ModelSetupRequestError(409, 88));
    getModelPull
      .mockResolvedValueOnce(setupPull({
        id: 88,
        operation_kind: 'local_model',
        model: 'llama3:8b',
        display_name: 'Llama 3',
      }))
      .mockResolvedValueOnce(setupPull({
        id: 88,
        operation_kind: 'local_model',
        model: 'llama3:8b',
        display_name: 'Llama 3',
        status: 'done',
        finished_at: '2026-10-16T00:01:00Z',
      }));

    render(<PaletteHarness />);
    fireEvent.click(await screen.findByTestId('search-setup'));
    fireEvent.click(screen.getByTestId('search-setup-confirm'));

    expect(await screen.findByTestId('search-setup-blocked-status')).toHaveTextContent(
      'Another setup operation is active',
    );
    expect(screen.queryByText('Setting up semantic search')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('search-setup-consent')).toBeVisible(), {
      timeout: 2_500,
    });
    expect(screen.getByTestId('search-setup-confirm')).toBeEnabled();
    expect(searchProject).toHaveBeenCalledTimes(1);
  });

  it('dismisses semantic setup without starting a download', async () => {
    searchProject.mockResolvedValue({
      groups: [],
      coverage: {
        requested_mode: 'hybrid',
        effective_modes: ['lexical'],
        state: 'unavailable',
        searched_channels: ['text'],
        channels: [{
          channel: 'semantic', state: 'unavailable', reason: 'model unavailable', recipe_id: null,
          setup: {
            setup_ref: 'engine-setup:embeddinggemma-2.local@1', scope: 'workspace',
            display_name: 'EmbeddingGemma 2', download_bytes: 1_525_784_180,
            additional_runtime_download: true, license: 'Apache-2.0',
            source_url: 'https://example.test/embeddinggemma',
          },
        }],
        is_top_k: true,
        truncated: false,
      },
    });
    render(<PaletteHarness />);

    fireEvent.click(await screen.findByTestId('search-setup'));
    fireEvent.click(screen.getByTestId('search-setup-not-now'));

    expect(screen.queryByTestId('search-setup-consent')).not.toBeInTheDocument();
    expect(startEngineSetup).not.toHaveBeenCalled();
  });

  it('offers preparation from exact current-media coverage without calling it searched', async () => {
    searchProject.mockResolvedValue({
      groups: [],
      coverage: {
        requested_mode: 'lexical',
        effective_modes: ['lexical'],
        state: 'partial',
        searched_channels: ['lexical'],
        channels: [{
          channel: 'lexical', state: 'ready', recipe_id: null, reason: null,
          eligible_count: null, prepared_count: null, failed_count: null, setup: null,
        }, {
          channel: 'search_preparation', state: 'unavailable', recipe_id: null,
          reason: 'Current media needs search preparation.',
          eligible_count: 1, prepared_count: 0, failed_count: null, setup: null,
          gap_sheet_ids: [1],
        }],
        is_top_k: true,
        truncated: false,
      },
    });
    render(<PaletteHarness />);

    const gap = await screen.findByTestId('search-preparation-gap');
    expect(gap).toHaveTextContent('1 current media item needs search preparation.');
    expect(screen.getByRole('status')).toHaveTextContent('Showing available search results.');
    expect(screen.queryByText(/while more material is prepared/i)).not.toBeInTheDocument();
  });

  it.each(['hybrid', 'semantic', 'lexical'] as const)(
    'explains unavailable semantic runtime in %s mode alongside media preparation',
    async (requestedMode) => {
      const reason = 'Semantic search is unavailable in this SQLite runtime. Install a supported Frisket runtime to enable it.';
      searchProject.mockResolvedValue({
        ...completeResponse,
        coverage: {
          ...completeResponse.coverage, requested_mode: requestedMode,
          effective_modes: ['lexical'], state: 'partial',
          channels: [{ channel: 'semantic', state: 'unavailable', reason, setup: null }, {
            channel: 'search_preparation', state: 'unavailable', eligible_count: 1,
            prepared_count: 0, gap_sheet_ids: [1],
          }],
        },
      });
      if (requestedMode === 'semantic') {
        const batch = {
          schemaVersion: 'frisket.search_preparation.v1', id: 'prep-runtime', sheet_id: 1,
          plan_hash: 'sha256:runtime', status: 'needs_consent',
          groups: [{ kind: 'ocr', count: 1, status: 'needs_consent' }],
          models: [], jobs: [], can_stop: false, can_setup: true, errors: [],
        };
        listSearchPreparations.mockResolvedValue({ batches: [batch] });
        getSearchPreparation.mockResolvedValue(batch);
      }
      render(<PaletteHarness />);
      expect(await screen.findByTestId('search-results')).toBeVisible();
      if (requestedMode === 'lexical') {
        expect(screen.queryByTestId('search-semantic-unavailable')).not.toBeInTheDocument();
        expect(screen.getByRole('status')).toHaveTextContent('Showing available search results.');
      } else {
        const notice = screen.getByTestId('search-semantic-unavailable');
        expect(notice).toHaveTextContent(reason);
        expect(notice).toHaveTextContent('Keyword search is available.');
        expect(screen.queryByTestId('search-setup')).not.toBeInTheDocument();
      }
      if (requestedMode === 'semantic') {
        expect(screen.getByTestId('search-preparation-receipt')).toBeVisible();
      } else {
        expect(screen.getByTestId('search-preparation-gap')).toHaveTextContent('1 current media item needs search preparation.');
        expect(screen.getByTestId('search-preparation-gap-action')).toHaveTextContent('Prepare Documents');
      }
    },
  );

  it('does not turn a semantic runtime problem into a file preparation gap', async () => {
    searchProject.mockResolvedValue({
      ...completeResponse,
      coverage: {
        ...completeResponse.coverage, requested_mode: 'hybrid', effective_modes: ['lexical'],
        state: 'partial', channels: [{
          channel: 'semantic', state: 'unavailable', setup: null,
          reason: 'Install a supported Frisket runtime to enable semantic search.',
        }],
      },
    });
    render(<PaletteHarness />);

    expect(await screen.findByTestId('search-semantic-unavailable')).toHaveTextContent('Keyword search is available.');
    expect(screen.queryByTestId('search-preparation-gap')).not.toBeInTheDocument();
  });

  it('prepares the sheet named by project coverage instead of the unrelated active sheet', async () => {
    searchProject.mockResolvedValue({
      groups: [],
      coverage: {
        requested_mode: 'lexical', effective_modes: ['lexical'], state: 'partial',
        searched_channels: ['lexical'], is_top_k: true, truncated: false,
        channels: [{
          channel: 'search_preparation', state: 'unavailable', recipe_id: null,
          reason: 'Current media needs search preparation.', eligible_count: 1,
          prepared_count: 0, failed_count: null, setup: null, gap_sheet_ids: [2],
        }],
      },
    });
    createSearchPreparation.mockReturnValue(new Promise(() => {}));
    render(<PaletteHarness
      searchSheets={[
        { id: '1', name: 'Dispatches' } as never,
        { id: '2', name: 'Court filings' } as never,
      ]}
      activeSearchSheetId="1"
    />);

    const action = await screen.findByTestId('search-preparation-gap-action');
    expect(action).toHaveTextContent('Prepare Court filings');
    fireEvent.click(action);

    await waitFor(() => expect(createSearchPreparation).toHaveBeenCalledWith('project-1', { sheet_id: 2 }, { signal: expect.any(AbortSignal) }));
  });

  it('ignores late initial approval restoration after preparing a different sheet', async () => {
    let restore!: (value: unknown) => void;
    let created!: (value: unknown) => void;
    const batchB = {
      id: 'prep-b', sheet_id: 2, plan_hash: 'sha256:b', status: 'needs_consent', force_ocr: false,
      groups: [{ kind: 'ocr', count: 1, status: 'needs_consent' }],
      models: [], jobs: [], can_stop: false, can_setup: true, errors: [],
    };
    listSearchPreparations.mockReturnValueOnce(new Promise((resolve) => { restore = resolve; }))
      .mockResolvedValue({ batches: [] });
    createSearchPreparation.mockReturnValue(new Promise((resolve) => { created = resolve; }));
    getSearchPreparation.mockResolvedValue(batchB);
    searchProject.mockResolvedValue({ ...completeResponse, coverage: { ...completeResponse.coverage,
      state: 'partial', channels: [{ channel: 'search_preparation', state: 'unavailable', eligible_count: 1, prepared_count: 0, gap_sheet_ids: [2] }],
    } });
    render(<PaletteHarness searchSheets={[{ id: '2', name: 'Court filings' } as never]} activeSearchSheetId="2" />);
    fireEvent.click(await screen.findByTestId('search-preparation-gap-action'));
    await waitFor(() => expect(createSearchPreparation).toHaveBeenCalledOnce());
    await act(async () => { restore({ batches: [{ ...batchB, id: 'prep-a', sheet_id: 1 }] }); });
    expect(getSearchPreparation).not.toHaveBeenCalled();
    await act(async () => { created(batchB); });
    await screen.findByTestId('search-preparation-decision-prepare');
    expect(getSearchPreparation).toHaveBeenCalledWith('project-1', 'prep-b');
    expect(getSearchPreparation).not.toHaveBeenCalledWith('project-1', 'prep-a');
  });

  it('restores approval only for the current sheet when Search opens', async () => {
    const awaitingApproval = {
      schemaVersion: 'frisket.search_preparation.v1',
      id: 'prep-awaiting-approval',
      sheet_id: 2,
      plan_hash: 'sha256:approval',
      status: 'needs_consent',
      groups: [{ kind: 'ocr', count: 1, status: 'needs_consent' }],
      models: [],
      jobs: [],
      can_stop: false,
      can_setup: true,
      errors: [],
    };
    searchProject.mockResolvedValue(completeResponse);
    listSearchPreparations.mockResolvedValue({
      batches: [
        { ...awaitingApproval, id: 'prep-other-sheet', sheet_id: 1 },
        { ...awaitingApproval, id: 'prep-complete', status: 'complete' },
        awaitingApproval,
      ],
    });
    getSearchPreparation.mockImplementation(async (_project, id) => ({ ...awaitingApproval, id }));

    render(<PaletteHarness activeSearchSheetId="2" />);

    expect(await screen.findByTestId('search-preparation-receipt')).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Make these files searchable' })).toBeVisible();
    expect(screen.getByTestId('search-preparation-model-consent')).toBeVisible();
    expect(listSearchPreparations).toHaveBeenCalledWith(
      'project-1',
      { signal: expect.any(AbortSignal) },
    );
    expect(getSearchPreparation).toHaveBeenCalledWith(
      'project-1',
      'prep-awaiting-approval',
    );
    expect(getSearchPreparation).not.toHaveBeenCalledWith('project-1', 'prep-other-sheet');
  });

  it('labels source results with their file and exact page', async () => {
    const documentHit = {
      ...hit('document:2', '<b>budget</b> on second page'),
      source_name: 'Council budget.pdf',
      location: { ...location, kind: 'document_page', pages: { start: 2, end: 2 } },
    };
    searchProject.mockResolvedValue({
      ...completeResponse,
      groups: [{ group_key: 'document', primary: documentHit, supporting: [], additional_count: 0 }],
    });
    render(<PaletteHarness />);

    const row = await screen.findByTestId('search-hit');
    expect(row).toHaveTextContent('Council budget.pdf');
    expect(row).toHaveTextContent('Page 2');
    expect(row).toHaveTextContent('budget on second page');
  });

  it('keeps the text choice in Search until explicit Prepare, then returns to the workspace', async () => {
    const pending = {
      schemaVersion: 'frisket.search_preparation.v1', id: 'prep-review', sheet_id: 1,
      plan_hash: 'sha256:review', force_ocr: false, status: 'needs_consent',
      groups: [{ kind: 'ocr', count: 1, status: 'needs_consent' }],
      models: [], jobs: [], can_stop: false, can_setup: true, errors: [],
    };
    listSearchPreparations.mockResolvedValue({ batches: [pending] });
    getSearchPreparation.mockResolvedValue(pending);
    decideSearchPreparation.mockResolvedValue({ ...pending, status: 'preparing' });
    searchProject.mockResolvedValue(completeResponse);
    const onClose = vi.fn();
    render(<PaletteHarness onClose={onClose} />);
    await screen.findByTestId('search-preparation-decision-prepare');
    expect(onClose).not.toHaveBeenCalled();
    expect(decideSearchPreparation).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('search-preparation-decision-prepare'));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(decideSearchPreparation).toHaveBeenCalledWith('project-1', 'prep-review', {
      plan_hash: 'sha256:review', decision: 'prepare', selected_setup_refs: [],
    });
    expect(screen.queryByTestId('search-preparation-receipt')).not.toBeInTheDocument();
  });

  it('returns to the workspace for already running preparation without creating a plan or reading samples', async () => {
    const running = { id: 'prep-running', sheet_id: 1, status: 'preparing' };
    listSearchPreparations.mockResolvedValue({ batches: [running] });
    searchProject.mockResolvedValue({ ...completeResponse, coverage: { ...completeResponse.coverage,
      state: 'partial', channels: [{ channel: 'search_preparation', state: 'unavailable', eligible_count: 1, prepared_count: 0, gap_sheet_ids: [1] }],
    } });
    const onClose = vi.fn();
    render(<PaletteHarness onClose={onClose} />);
    fireEvent.click(await screen.findByTestId('search-preparation-gap-action'));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(createSearchPreparation).not.toHaveBeenCalled();
    expect(getSearchPreparationTextSamples).not.toHaveBeenCalled();
    expect(screen.queryByTestId('search-preparation-receipt')).not.toBeInTheDocument();
  });

  it('selects a named index for free-text search and saves its semantic view', async () => {
    searchProject.mockResolvedValue(completeResponse);
    render(<PaletteHarness />);

    const indexSelect = await screen.findByTestId('search-index-select');
    await waitFor(() => expect(indexSelect).toHaveTextContent('Research passages'));
    fireEvent.change(indexSelect, { target: { value: 'index-1' } });
    fireEvent.change(screen.getByTestId('search-mode-select'), {
      target: { value: 'semantic' },
    });

    await waitFor(() => expect(searchProject.mock.calls.at(-1)?.[1]).toMatchObject({
      named_index_id: 'index-1',
      mode: 'semantic',
      group_by: 'row',
      query: { kind: 'text', text: 'budget' },
    }));
    fireEvent.click(screen.getByTestId('save-search-lens-button'));

    await waitFor(() => expect(saveLens).toHaveBeenCalledWith({
      name: 'Search: budget',
      query: {
        kind: 'embedding_similarity',
        embedding_index_id: 'index-1',
        sheet_id: 1,
        anchor: {
          kind: 'manual_text_query',
          terms: [{ text: 'budget', weight: 1 }],
        },
      },
    }));
    expect(await screen.findByTestId('save-search-lens-status')).toHaveTextContent('Saved view');
  });

  it('creates a persisted row-similarity watch from a named-index anchor', async () => {
    searchProject.mockResolvedValue(completeResponse);
    render(<AnchoredPaletteHarness />);

    const watch = await screen.findByTestId('watch-search-button');
    await waitFor(() => expect(watch).toBeEnabled());
    fireEvent.click(watch);

    await waitFor(() => expect(createWatch).toHaveBeenCalledWith({
      name: 'Similar to page 2',
      scope: { kind: 'sheet', sheet_id: 1 },
      query: {
        kind: 'embedding_similarity',
        embedding_index_id: 'index-1',
        anchor: { kind: 'row', row_id: 2 },
      },
    }));

    fireEvent.click(screen.getByTestId('save-search-lens-button'));
    await waitFor(() => expect(saveLens).toHaveBeenCalledWith({
      name: 'Similar to page 2',
      query: {
        kind: 'embedding_similarity',
        embedding_index_id: 'index-1',
        sheet_id: 1,
        anchor: { kind: 'row', row_id: 2 },
        limit: 500,
      },
    }));
  });

  it('keeps project keyword watches available', async () => {
    searchProject.mockResolvedValue(completeResponse);
    render(<PaletteHarness />);

    fireEvent.change(screen.getByTestId('search-mode-select'), {
      target: { value: 'lexical' },
    });
    const watch = screen.getByTestId('watch-search-button');
    await waitFor(() => expect(watch).toBeEnabled());
    fireEvent.click(watch);

    await waitFor(() => expect(createWatch).toHaveBeenCalledWith({
      name: 'Search: budget',
      scope: { kind: 'project' },
      query: { kind: 'fts', q: 'budget', mode: 'keyword', rerank: 'off' },
    }));
  });
});
