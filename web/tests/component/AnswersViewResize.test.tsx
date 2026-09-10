// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ColumnDef, SheetMeta } from '../../src/api/types';
import { AnswersView } from '../../src/workbench/AnswersView';

vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({ projectApi: {} }),
}));

const PROJECT_ID = 'project-answers-resize';
const STORAGE_KEY = `frisket:answers-evidence-width:${PROJECT_ID}`;
const ANSWER_COLUMN: ColumnDef = {
  id: 'answer',
  name: 'Answer',
  type: 'text',
};
const SHEET: SheetMeta = {
  id: 'sheet-answers',
  name: 'Answers',
  rowCount: 0,
  columns: [ANSWER_COLUMN],
};

function renderAnswersView() {
  return render(
    <AnswersView
      projectId={PROJECT_ID}
      sheet={SHEET}
      citedColumns={[ANSWER_COLUMN]}
      chosenColumnId={ANSWER_COLUMN.id}
      onChangeColumn={vi.fn()}
      activeLinkId={null}
      onSetActiveLink={vi.fn()}
      selectedRowIds={[]}
      onSyncSelectRow={vi.fn()}
      queryRows={vi.fn().mockResolvedValue({ rows: [], total: 0 })}
      orderKey="sheet-order"
    />,
  );
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
  vi.clearAllMocks();
});

describe('Answers evidence pane resizing', () => {
  it('renders all three panes around an accessible bounded resize seam', () => {
    renderAnswersView();

    const view = screen.getByTestId('grounded-answers-view');
    expect(screen.getByTestId('answers-row-list')).toBeInTheDocument();
    expect(screen.getByTestId('answers-column')).toBeInTheDocument();
    expect(screen.getByTestId('answers-evidence-pane')).toBeInTheDocument();
    expect(view).toHaveStyle('--answers-evidence-width: 600px');
    expect(view).toHaveStyle('--answers-evidence-min-width: 420px');
    expect(view).toHaveStyle('--answers-evidence-max-width: 960px');

    const seam = screen.getByRole('separator', {
      name: 'Resize the Answers evidence pane',
    });
    expect(seam).toHaveAttribute('aria-orientation', 'vertical');
    expect(seam).toHaveAttribute('aria-valuemin', '420');
    expect(seam).toHaveAttribute('aria-valuemax', '960');
    expect(seam).toHaveAttribute('aria-valuenow', '600');
    expect(seam).toHaveAttribute('tabindex', '0');
  });

  it('resizes by keyboard and persists the project-scoped preference', () => {
    renderAnswersView();
    const seam = screen.getByTestId('answers-evidence-seam');

    fireEvent.keyDown(seam, { key: 'ArrowRight' });
    expect(seam).toHaveAttribute('aria-valuenow', '576');
    expect(localStorage.getItem(STORAGE_KEY)).toBe('576');

    fireEvent.keyDown(seam, { key: 'ArrowLeft', shiftKey: true });
    expect(seam).toHaveAttribute('aria-valuenow', '624');
    expect(localStorage.getItem(STORAGE_KEY)).toBe('624');
  });

  it('resizes by pointer and clamps an oversized stored preference', () => {
    localStorage.setItem(STORAGE_KEY, '5000');
    const { unmount } = renderAnswersView();
    let seam = screen.getByTestId('answers-evidence-seam');
    expect(seam).toHaveAttribute('aria-valuenow', '960');
    unmount();

    localStorage.removeItem(STORAGE_KEY);
    renderAnswersView();
    seam = screen.getByTestId('answers-evidence-seam');
    fireEvent.pointerDown(seam, { clientX: 700 });
    expect(screen.getByTestId('grounded-answers-view')).toHaveClass(
      'answers-view-resizing',
    );
    fireEvent.pointerMove(window, { clientX: 600 });
    expect(seam).toHaveAttribute('aria-valuenow', '700');
    fireEvent.pointerUp(window);

    expect(screen.getByTestId('grounded-answers-view')).not.toHaveClass(
      'answers-view-resizing',
    );
    expect(localStorage.getItem(STORAGE_KEY)).toBe('700');

    fireEvent.pointerDown(seam, { clientX: 600 });
    fireEvent.pointerMove(window, { clientX: 2000 });
    fireEvent.pointerUp(window);
    expect(seam).toHaveAttribute('aria-valuenow', '420');
    expect(localStorage.getItem(STORAGE_KEY)).toBe('420');
  });
});
