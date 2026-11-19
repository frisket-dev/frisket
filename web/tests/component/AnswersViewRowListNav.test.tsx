// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ColumnDef, Row, SheetMeta } from '../../src/api/types';
import { AnswersView } from '../../src/workbench/AnswersView';

vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({
    projectApi: { getColumnEvidence: () => Promise.resolve({ rows: [] }) },
  }),
}));

const NAME_COLUMN: ColumnDef = { id: 'name', name: 'Name', type: 'text' };
const ANSWER_COLUMN: ColumnDef = { id: 'answer', name: 'Answer', type: 'text' };
const SHEET: SheetMeta = {
  id: 'sheet-answers',
  name: 'Answers',
  rowCount: 3,
  columns: [NAME_COLUMN, ANSWER_COLUMN],
  citedColumnIds: [ANSWER_COLUMN.id],
  annotatedTextColumnIds: [],
  documentTextColumnIds: [],
};
const ROWS: Row[] = ['Acme Corp', 'Beta LLC', 'Gamma Inc'].map((name, index) => ({
  id: String(index + 1),
  index,
  cells: { name, answer: `answer ${index + 1}` },
  provenance: {},
}));

type QueryRows = (args: { columnIds?: string[]; offset: number; limit: number }) => Promise<{
  rows: Row[];
  total: number;
}>;

// Mirrors the workspace host: the selection lives above AnswersView, and the
// title column order is a fresh array on every host render.
function SelectionSyncedHost({ queryRows }: { queryRows: QueryRows }) {
  const [selectedRowIds, setSelectedRowIds] = useState<string[]>([]);
  return (
    <AnswersView
      projectId="project-answers-nav"
      sheet={SHEET}
      citedColumns={[ANSWER_COLUMN]}
      chosenColumnId={ANSWER_COLUMN.id}
      onChangeColumn={vi.fn()}
      activeLinkId={null}
      onSetActiveLink={vi.fn()}
      selectedRowIds={selectedRowIds}
      onSyncSelectRow={(rowId) => setSelectedRowIds([rowId])}
      queryRows={queryRows}
      orderKey="sheet-order"
      titleColumnOrder={[NAME_COLUMN.name, ANSWER_COLUMN.name]}
    />
  );
}

function activeRowIds(): (string | null)[] {
  return screen
    .queryAllByTestId('answers-row-item')
    .filter((item) => item.getAttribute('data-active') === 'true')
    .map((item) => item.getAttribute('data-row-id'));
}

beforeEach(() => {
  vi.stubGlobal(
    'ResizeObserver',
    class ResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe('AnswersView row list arrow navigation', () => {
  it('moves one row per key press when presses arrive before any row reload could settle', async () => {
    const queryRows = vi.fn<QueryRows>().mockResolvedValue({ rows: ROWS, total: ROWS.length });
    render(<SelectionSyncedHost queryRows={queryRows} />);
    expect(await screen.findAllByTestId('answers-row-item')).toHaveLength(3);
    expect(activeRowIds()).toEqual(['1']);

    const listBody = screen.getByTestId('answers-row-list-body');
    fireEvent.keyDown(listBody, { key: 'ArrowDown' });
    fireEvent.keyDown(listBody, { key: 'ArrowDown' });
    expect(activeRowIds()).toEqual(['3']);

    fireEvent.keyDown(listBody, { key: 'ArrowUp' });
    fireEvent.keyDown(listBody, { key: 'ArrowUp' });
    expect(activeRowIds()).toEqual(['1']);

    expect(queryRows).toHaveBeenCalledTimes(1);
  });
});
