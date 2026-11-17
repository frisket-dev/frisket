// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { createProjectApi } from '../../src/api/real';
import type { SheetMeta } from '../../src/api/types';
import { SheetInfoPopover } from '../../src/workspace/SheetInfoPopover';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const { render } = createWorkspaceTestHarness({
  projectId: 'sheet-info-project',
  api: { projectApi: createProjectApi('sheet-info-project') },
});

afterEach(() => cleanup());

function derivedSheet(refreshable: boolean): SheetMeta {
  return {
    id: '2',
    name: 'Extracted records',
    rowCount: 4,
    columns: [],
    citedColumnIds: [],
    annotatedTextColumnIds: [],
    dependentSheetIds: [],
    parent: { sheetId: '1', sheetName: 'Documents', viaAction: 'Extract documents' },
    syncState: 'synced',
    refreshable,
  };
}

it('keeps source freshness separate from unavailable rerun controls', () => {
  render(<SheetInfoPopover
    sheet={derivedSheet(false)}
    onClose={vi.fn()}
    onNavigateParent={vi.fn()}
    onViewLineage={vi.fn()}
    refreshSheets={vi.fn()}
  />);

  expect(screen.getByTestId('sheet-info-badge')).toHaveTextContent('Up to date');
  expect(screen.queryByTestId('sheet-info-rerun')).not.toBeInTheDocument();
  expect(screen.queryByTestId('sheet-info-rerun-scope')).not.toBeInTheDocument();
  expect(screen.getByTestId('sheet-info-view-lineage')).toBeInTheDocument();
  expect(screen.getByTestId('sheet-info-transform')).toHaveTextContent('Extract documents');
});

it('labels manually refreshable outputs without implying automatic updates', () => {
  render(<SheetInfoPopover
    sheet={derivedSheet(true)}
    onClose={vi.fn()}
    onNavigateParent={vi.fn()}
    onViewLineage={vi.fn()}
    refreshSheets={vi.fn()}
  />);

  expect(screen.getByTestId('sheet-info-badge')).toHaveTextContent('Up to date');
  expect(screen.getByTestId('sheet-info-rerun')).toBeInTheDocument();
  expect(screen.getByTestId('sheet-info-rerun-scope')).toBeInTheDocument();
  expect(screen.getByTestId('sheet-info-badge')).not.toHaveTextContent('Live');
});

it.each([
  ['unknown', 'Source changes not checked'],
  ['checking', 'Checking source changes…'],
] as const)('does not present %s source freshness as up to date', (syncState, label) => {
  render(<SheetInfoPopover
    sheet={{ ...derivedSheet(false), syncState }}
    onClose={vi.fn()}
    onNavigateParent={vi.fn()}
    onViewLineage={vi.fn()}
    refreshSheets={vi.fn()}
  />);
  expect(screen.getByTestId('sheet-info-badge')).toHaveTextContent(label);
  expect(screen.getByTestId('sheet-info-badge')).toHaveAttribute('data-tone', 'neutral');
  expect(screen.getByTestId('sheet-info-badge')).not.toHaveTextContent('Up to date');
});

it('excludes non-refreshable stale sheets from cascade actions', async () => {
  const { staleSheetsDeepestFirst } = await import('../../src/workbench/lineageStale');
  expect(staleSheetsDeepestFirst([
    { ...derivedSheet(false), id: 'old', syncState: 'stale' },
    { ...derivedSheet(true), id: 'current', syncState: 'stale' },
  ]).map(sheet => sheet.id)).toEqual(['current']);
});
