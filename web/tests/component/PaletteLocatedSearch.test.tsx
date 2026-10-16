// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WorkbenchCommandPalette } from '../../src/workbench/WorkbenchCommandPalette';

const searchProject = vi.fn();
const startEngineSetup = vi.fn();
const embeddingIndexes = vi.fn();
const saveLens = vi.fn();
const createWatch = vi.fn();
const projectApi = { embeddingIndexes, saveLens, createWatch };

vi.mock('../../src/api/open', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/open')>(),
  searchProject: (...args: unknown[]) => searchProject(...args),
}));

vi.mock('../../src/api/engineSetup', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/engineSetup')>(),
  startEngineSetup: (...args: unknown[]) => startEngineSetup(...args),
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

function PaletteHarness() {
  const [query, setQuery] = useState('budget');
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
      bestMatchItems={[]}
      actionItems={[]}
      gotoItems={[]}
      onLaunchAction={vi.fn()}
      onNavigateSheet={vi.fn()}
      searchSheets={[{ id: '1', name: 'Documents' } as never]}
      activeSearchSheetId="1"
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

  it('discloses the semantic model download before starting setup', async () => {
    searchProject.mockResolvedValue({
      groups: [],
      coverage: {
        requested_mode: 'hybrid',
        effective_modes: ['lexical'],
        state: 'unavailable',
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
    startEngineSetup.mockResolvedValue({});
    render(<PaletteHarness />);

    fireEvent.click(await screen.findByTestId('search-setup'));
    expect(startEngineSetup).not.toHaveBeenCalled();
    const consent = screen.getByTestId('search-setup-consent');
    expect(consent).toHaveTextContent('Download EmbeddingGemma 2?');
    expect(consent).toHaveTextContent('1.5 GB');
    expect(consent).toHaveTextContent(/Additional runtime files/);
    fireEvent.click(screen.getByTestId('search-setup-confirm'));
    await waitFor(() => expect(startEngineSetup).toHaveBeenCalledWith(
      'workspace',
      'engine-setup:embeddinggemma-2.local@1',
    ));
    await waitFor(() => expect(searchProject).toHaveBeenCalledTimes(2));
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
