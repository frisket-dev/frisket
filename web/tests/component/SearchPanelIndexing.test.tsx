// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SearchPanel } from '../../src/components/SearchPanel';

const searchProject = vi.fn();

vi.mock('../../src/api/open', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/open')>(),
  searchProject: (...args: unknown[]) => searchProject(...args),
}));

vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({
    chromePreferences: { projectId: 'project-1' },
    projectApi: { createWatch: vi.fn() },
  }),
}));

beforeEach(() => {
  vi.useFakeTimers();
  searchProject.mockReset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('partial project search results', () => {
  it('reports indexing without claiming an empty result and refreshes until complete', async () => {
    searchProject
      .mockResolvedValueOnce({ hits: [], indexing: true })
      .mockResolvedValueOnce({
        hits: [{
          sheet_id: 1,
          row_id: 2,
          column_id: 3,
          column_name: 'body',
          snip: '<b>budget</b>',
        }],
        indexing: false,
      });

    render(<SearchPanel sheets={[{ id: '1', name: 'Documents' } as never]} onPick={vi.fn()} />);
    fireEvent.click(screen.getByTestId('open-search'));
    fireEvent.change(screen.getByTestId('search-input'), { target: { value: 'budget' } });

    await act(async () => { await vi.advanceTimersByTimeAsync(180); });
    expect(screen.getByRole('status')).toHaveTextContent('Indexing… results may be incomplete.');
    expect(screen.queryByText(/No matches for/)).not.toBeInTheDocument();

    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(screen.getByTestId('search-hit')).toHaveTextContent('budget');
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(searchProject).toHaveBeenCalledTimes(2);
  });
});
