// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/media/pdfjsSetup', () => ({
  pdfjsLib: { getDocument: () => {}, TextLayer: class {} },
}));

import type { Row, SheetMeta } from '../../src/api/types';
import type { DocumentViewState } from '../../src/workspace/useWorkspaceChromeState';
import { DocumentView } from '../../src/workbench/DocumentView';

afterEach(cleanup);

const sheet: SheetMeta = {
  id: 'sheet-1',
  name: 'Filings',
  rowCount: 1,
  columns: [
    { id: 'file', name: 'file', type: 'file', ai_generated: false },
  ],
  citedColumnIds: [],
  annotatedTextColumnIds: [],
};

const state: DocumentViewState = {
  sheetId: sheet.id,
  sourceColumnId: null,
  titleColumnId: null,
  layout: 'continuous',
  fit: 'width',
  videoFit: 'full',
  textLayer: true,
  sync: false,
  activeRowId: null,
};

describe('DocumentView loading state', () => {
  it('does not render an empty reader while the initial row page is pending', async () => {
    let resolveRows!: (page: { rows: Row[]; total: number }) => void;
    const queryRows = () => new Promise<{ rows: Row[]; total: number }>((resolve) => {
      resolveRows = resolve;
    });

    render(
      <DocumentView
        projectId="project-1"
        sheet={sheet}
        state={state}
        onChangeState={() => undefined}
        onDocumentFocus={() => undefined}
        onOpenDetail={() => undefined}
        queryRows={queryRows}
        onListSort={() => undefined}
        orderKey="none"
        listSortDir={null}
        annotatedTextColumnIds={[]}
        disabledToggleKeys={[]}
        onSetDisabledToggleKeys={() => undefined}
      />,
    );

    expect(screen.getByTestId('document-view-loading')).toHaveTextContent('Loading documents…');
    expect(screen.queryByTestId('document-empty')).toBeNull();

    await act(async () => {
      resolveRows({ rows: [], total: 0 });
    });
    await waitFor(() => expect(screen.queryByTestId('document-view-loading')).toBeNull());
    expect(screen.getByTestId('document-empty')).toBeVisible();
  });
});
