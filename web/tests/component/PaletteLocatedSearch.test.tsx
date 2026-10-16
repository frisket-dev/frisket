// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WorkbenchCommandPalette } from '../../src/workbench/WorkbenchCommandPalette';

const searchProject = vi.fn();
const startEngineSetup = vi.fn();

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
    projectApi: { createWatch: vi.fn() },
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
      searchSeed={{
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
});

afterEach(cleanup);

describe('located palette search', () => {
  it('puts an explicit similarity anchor ahead of unrelated palette commands', async () => {
    searchProject.mockResolvedValue(completeResponse);
    render(<AnchoredPaletteHarness />);

    expect(await screen.findByTestId('search-anchor')).toHaveTextContent('Similar to page 2');
    expect(screen.queryByText('Extract fields')).not.toBeInTheDocument();

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

  it('starts the structured setup action and retries the same request', async () => {
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
          setup: { setup_ref: 'engine-setup:embeddinggemma-2.local@1', scope: 'workspace' },
        }],
        is_top_k: true,
        truncated: false,
      },
    });
    startEngineSetup.mockResolvedValue({});
    render(<PaletteHarness />);

    fireEvent.click(await screen.findByTestId('search-setup'));
    await waitFor(() => expect(startEngineSetup).toHaveBeenCalledWith(
      'workspace',
      'engine-setup:embeddinggemma-2.local@1',
    ));
    await waitFor(() => expect(searchProject).toHaveBeenCalledTimes(2));
  });
});
