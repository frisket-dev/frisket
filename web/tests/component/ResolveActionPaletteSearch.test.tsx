// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { WorkbenchCommandPalette } from '../../src/workbench/WorkbenchCommandPalette';
import { completeCatalogPayload } from '../support/actionFormFixtures';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';

vi.mock('../../src/workbench/usePaletteSearch', () => ({
  usePaletteSearch: () => ({
    mode: 'keyword',
    setMode: vi.fn(),
    rerank: false,
    setRerank: vi.fn(),
    hits: null,
    searchError: null,
    canWatch: false,
    watchBusy: false,
    watchStatus: null,
    createSearchWatch: vi.fn(),
    searchGroups: [],
    sheetName: vi.fn(),
  }),
}));

const RESOLVE_IDS = new Set([
  'resolve.substitute',
  'resolve.replace',
  'resolve.combine',
  'resolve.fill_missing',
]);
const generatedResolveEntries = ['resolve.substitute', 'resolve.replace', 'resolve.combine']
  .map((kind) => syntheticActionCatalogEntry(kind, {
    ui_hints: {
      form: 'generated',
      category: 'resolve',
      semantic_controls: {},
      logical_outputs: [{ key: 'cleaned', column_type: 'text' }],
    },
  }));
const actionItems = actionTemplatesFromCatalog(
  completeCatalogPayload([], generatedResolveEntries),
)
  .filter((template) => RESOLVE_IDS.has(template.kind))
  .map((template) => ({
    actionKind: template.kind,
    name: template.name,
    keywords: template.keywords,
  }));

function PaletteHarness() {
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
      bestMatchItems={[]}
      actionItems={actionItems}
      gotoItems={[]}
      onLaunchAction={vi.fn()}
      onNavigateSheet={vi.fn()}
      searchSheets={[]}
      onNavigateSearchHit={vi.fn()}
    />
  );
}

afterEach(cleanup);

describe('resolve action command-palette vocabulary', () => {
  it.each([
    ['recode', 'resolve.substitute'],
    ['regex', 'resolve.replace'],
    ['group values', 'resolve.combine'],
    ['buckets', 'resolve.combine'],
    ['ffill', 'resolve.fill_missing'],
    ['impute', 'resolve.fill_missing'],
  ])('finds the canonical action for %s', (query, expectedActionId) => {
    render(<PaletteHarness />);
    fireEvent.change(screen.getByTestId('command-palette-input'), {
      target: { value: query },
    });

    const matches = screen.getAllByTestId('palette-action-item');
    expect(matches).toHaveLength(1);
    expect(matches[0]).toHaveAttribute('data-action-kind', expectedActionId);
  });
});
