// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ImportSessionStatus } from '../../src/api/open';
import { createProjectApi } from '../../src/api/real';
import { StatusBar } from '../../src/components/StatusBar';
import {
  importActivity,
  type ImportProgressController,
} from '../../src/components/useImportProgress';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const projectId = 'status-pdf-index-progress';
const { render } = createWorkspaceTestHarness({
  projectId,
  api: { projectApi: createProjectApi(projectId) },
});

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function importSession(): ImportSessionStatus {
  return {
    import_ref: 'import-00000000000000000000000000000000',
    state: 'completed',
    admitted_files: 2,
    admitted_bytes: 1_000,
    through: 2,
    committed_rows: 2,
    committed_bytes: 1_000,
    sheet_id: 7,
    sheet_name: 'PDFs',
    cancel_requested: false,
    sealed: true,
    error: null,
    search_preparation_status: 'running',
    search_preparation_batch_id: null,
    selected_total: 2,
    preparation_completed: 1,
    preparation_total: 2,
    ocr_completed: 0,
    ocr_total: 1,
    unreadable_total: 0,
    preparation_pending_confirmation: null,
    preparation_children: [],
  };
}

function statusBar(importProgress?: ImportProgressController) {
  return (
    <StatusBar
      run={null}
      history={null}
      reviewCount={0}
      projectionStatus={null}
      importProgress={importProgress}
      onUndo={vi.fn()}
      onRedo={vi.fn()}
      onOpenReview={vi.fn()}
      onCancelRun={vi.fn()}
    />
  );
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('StatusBar PDF indexing progress', () => {
  it('keeps HTTP page progress visible across polling and import activity composition', async () => {
    vi.useFakeTimers();
    let indexing = {
      status: 'running' as const,
      job_id: 2036,
      error: null,
      pdf_files: [{
        source_id: 'blob:doc-06',
        filename: 'Doc-06.pdf',
        completed: 11_448,
        total: 24_909,
      }],
      pdf_files_truncated: true,
    };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === `/api/projects/${projectId}/search-preparation`) {
        return jsonResponse({ batches: [], indexing });
      }
      if (path === '/api/providers/models/pulls') return jsonResponse({ pulls: [] });
      throw new Error(`Unexpected request: ${path}`);
    }));

    const view = render(statusBar());
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    fireEvent.click(screen.getByTestId('search-activity-toggle'));

    const pageProgress = screen.getByRole('progressbar', { name: 'Indexing pages for Doc-06.pdf' });
    expect(pageProgress).toHaveAttribute('max', '24909');
    expect(pageProgress).toHaveAttribute('value', '11448');
    expect(screen.getByText('11,448 / 24,909 pages · 13,461 remaining')).toBeVisible();
    expect(screen.getByText('More PDFs are being indexed.')).toBeVisible();

    indexing = { ...indexing, job_id: 2042, pdf_files: [{
      ...indexing.pdf_files[0], completed: 11_449,
    }] };
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(screen.getByRole('progressbar', { name: 'Indexing pages for Doc-06.pdf' }))
      .toHaveAttribute('value', '11449');
    fireEvent.click(screen.getByTestId('search-preparation-details'));
    expect(screen.getByText('Job 2,042')).toBeVisible();
    expect(screen.queryByText('Job 2,036')).not.toBeInTheDocument();

    const activity = importActivity(importSession());
    expect(activity).not.toBeNull();
    const importProgress: ImportProgressController = {
      activities: [activity!],
      message: null,
      busyRef: null,
      preparingRef: null,
      resume: vi.fn(),
      stop: vi.fn(async () => {}),
      keep: vi.fn(),
    };
    view.rerender(statusBar(importProgress));
    fireEvent.click(screen.getByTestId('search-activity-toggle'));

    expect(screen.getByRole('progressbar', { name: 'Indexing pages for Doc-06.pdf' }))
      .toHaveAttribute('value', '11449');
    expect(screen.getByText('More PDFs are being indexed.')).toBeVisible();
    expect(screen.getByText('✓ Done')).toBeVisible();
    expect(screen.getByText('✓ Read')).toBeVisible();
    expect(screen.getByRole('progressbar', { name: 'Search index progress' }))
      .toHaveAttribute('max', '24909');
  });
});
