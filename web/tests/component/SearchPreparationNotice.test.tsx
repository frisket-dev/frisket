// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ModelPullDto } from '../../src/api/types';
import type { SearchPreparationBatch } from '../../src/api/searchPreparation';
import * as preparationApi from '../../src/api/searchPreparation';
import * as openApi from '../../src/api/open';
import { SearchPreparationNotice } from '../../src/components/SearchPreparationNotice';
import {
  deriveSearchPreparationNotice,
  useSearchPreparationNotice,
  type SearchPreparationSnapshot,
} from '../../src/components/useSearchPreparationNotice';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function pull(overrides: Partial<ModelPullDto> = {}): ModelPullDto {
  return {
    schemaVersion: 'frisket.model_pull.v4',
    id: 983,
    display_name: 'EmbeddingGemma',
    operation_kind: 'engine_setup',
    capabilities: { cancel: true, retry: false, remove: false },
    endpoint_id: null,
    endpoint_origin: null,
    initiated_by: null,
    artifact: null,
    model: 'engine-setup:embeddinggemma-2.local@1',
    status: 'running',
    phase: 'downloading model files',
    total_bytes: 1_000,
    completed_bytes: 420,
    error: null,
    resolved_digest: null,
    resolved_size: null,
    created_at: '2026-10-16T12:00:00Z',
    started_at: '2026-10-16T12:00:01Z',
    finished_at: null,
    cancel_requested: false,
    ...overrides,
  };
}

function batch(overrides: Partial<SearchPreparationBatch> = {}): SearchPreparationBatch {
  return {
    schemaVersion: 'frisket.search_preparation.v1',
    id: 'prep-1',
    sheet_id: 7,
    plan_hash: 'sha256:plan',
    status: 'preparing',
    groups: [{ kind: 'ocr', count: 2, status: 'running' }],
    models: [],
    jobs: [{ job_id: 44, action_kind: 'document.ocr', status: 'running' }],
    can_stop: true,
    can_setup: false,
    errors: [],
    ...overrides,
  };
}

type PreparationProgress = NonNullable<SearchPreparationBatch['jobs'][number]['progress']>;

function preparationProgress(overrides: Partial<PreparationProgress> = {}): PreparationProgress {
  return {
    action_kind: 'document.ocr',
    action_name: 'Read PDF text',
    completed: 2,
    cost: null,
    failed: 1,
    live: true,
    run_id: 92,
    sheet_id: 7,
    status: 'running',
    timing: {
      elapsed_seconds: 3,
      estimated_finish_at: null,
      eta_seconds: 2,
      finished_at: null,
      job_elapsed_seconds: 3,
      processed_rows: 2,
      processed_rows_per_second: 0.5,
      queue_wait_seconds: 0,
      remaining_rows: 1,
      started_at: '2026-10-16T12:00:01Z',
    },
    total: 3,
    ...overrides,
  };
}

function snapshot(overrides: Partial<SearchPreparationSnapshot> = {}): SearchPreparationSnapshot {
  return {
    batches: [],
    indexing: { status: 'idle', job_id: null, error: null },
    pulls: [],
    ...overrides,
  };
}

describe('search preparation notice projection', () => {
  it('keeps running progress in activity without linking back to a progress dialog', () => {
    const notice = deriveSearchPreparationNotice(snapshot({ batches: [batch()] }));
    expect(notice).not.toBeNull();
    render(<SearchPreparationNotice notice={notice!} onDismiss={vi.fn()} onReviewPreparation={vi.fn()} />);
    expect(screen.queryByTestId('search-preparation-review')).toBeNull();
  });

  it('shows independent indexing bars for active PDFs alongside preparation and removes finished files', () => {
    const files = [
      { source_id: 'source:first', filename: 'Council papers.pdf', completed: 1_896, total: 24_909 },
      { source_id: 'source:second', filename: 'Short report.pdf', completed: 192, total: 196 },
    ];
    const notice = deriveSearchPreparationNotice(snapshot({ batches: [batch()],
      indexing: { status: 'running', job_id: 781, error: null, pdf_files: files, pdf_files_truncated: true },
    }));
    expect(notice).toMatchObject({ stage: 'prepare', progress: null });
    const view = render(<SearchPreparationNotice notice={notice!} />);
    const first = screen.getByRole('progressbar', { name: 'Indexing pages for Council papers.pdf' });
    expect(first).toHaveAttribute('max', '24909');
    expect(first).toHaveAttribute('value', '1896');
    expect(screen.getByRole('progressbar', { name: 'Indexing pages for Short report.pdf' })).toHaveAttribute('value', '192');
    expect(screen.getByText('1,896 / 24,909 pages')).toBeVisible();
    expect(screen.getByText('192 / 196 pages')).toBeVisible();
    expect(screen.getByText('More PDFs are being indexed.')).toBeVisible();
    expect(screen.queryByTestId('search-preparation-progress')).toBeNull();
    expect(screen.getByTestId('search-preparation-banner')).not.toHaveTextContent(/%|ETA|source:first/);

    const next = deriveSearchPreparationNotice(snapshot({ indexing: {
      status: 'queued', job_id: 782, error: null,
      pdf_files: [{ ...files[0], filename: null, completed: 1_897 }], pdf_files_truncated: false,
    } }));
    view.rerender(<SearchPreparationNotice notice={next!} />);
    expect(screen.queryByRole('progressbar', { name: /Short report/ })).toBeNull();
    expect(screen.getByRole('progressbar', { name: 'Indexing pages for PDF document' })).toHaveAttribute('value', '1897');
    expect(screen.queryByText('More PDFs are being indexed.')).toBeNull();

    const starting = deriveSearchPreparationNotice(snapshot({ indexing: { status: 'pending', job_id: null, error: null } }));
    view.rerender(<SearchPreparationNotice notice={starting!} />);
    expect(screen.queryByRole('progressbar')).toBeNull();
    expect(screen.getByTestId('search-preparation-banner')).toHaveTextContent('Waiting to index');
  });

  it('shows concurrent OCR page counters separately and drops only the finished file', () => {
    const currentBatch = batch({
      jobs: [{
        job_id: 44, action_kind: 'media.ocr', status: 'running',
        progress: preparationProgress({
          completed: 0, failed: 0,
          work_progress: {
            kind: 'ocr_pages',
            files: [
              { row_id: 12, completed: 3, total: 100, filename: 'first.pdf' },
              { row_id: 13, completed: 40, total: 500 },
            ],
          },
        }),
      }],
    });
    const notice = deriveSearchPreparationNotice(snapshot({ batches: [currentBatch] }));
    expect(notice).toMatchObject({ detail: 'Reading 2 files · page progress in Details', progress: null });
    const view = render(<SearchPreparationNotice notice={notice!} />);
    fireEvent.click(screen.getByTestId('search-preparation-details'));
    expect(screen.getByText('first.pdf · 3 of 100 pages')).toBeInTheDocument();
    expect(screen.getByText('File row 13 · 40 of 500 pages')).toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();

    const next = deriveSearchPreparationNotice(snapshot({ batches: [batch({
      jobs: [{
        ...currentBatch.jobs[0],
        progress: preparationProgress({
          completed: 1, failed: 0,
          work_progress: { kind: 'ocr_pages', files: [{ row_id: 13, completed: 41, total: 500 }] },
        }),
      }],
    })] }));
    view.rerender(<SearchPreparationNotice notice={next!} />);
    expect(screen.queryByText(/first.pdf/)).not.toBeInTheDocument();
    expect(screen.getByText('File row 13 · 41 of 500 pages')).toBeInTheDocument();
    expect(next?.detail).toBe('Reading file · 41 of 500 pages in current file');
  });

  it('preserves the failed activity identity through retry, warned completion, expiry and later polls', async () => {
    vi.useFakeTimers();
    const list = vi.spyOn(preparationApi, 'listSearchPreparations').mockResolvedValue({
      batches: [batch()], indexing: { status: 'idle', job_id: null, error: null },
    });
    vi.spyOn(openApi, 'listModelPulls').mockResolvedValue({
      schemaVersion: 'frisket.model_pull_list.v1', pulls: [],
    });
    const { result } = renderHook(() => useSearchPreparationNotice('project-1', false));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    list.mockResolvedValue({
      batches: [batch({ status: 'failed', groups: [{ kind: 'ocr', count: 1, status: 'failed' }],
        errors: ['Model setup failed.'] })], indexing: { status: 'idle', job_id: null, error: null },
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(result.current?.activityKey).toBe('preparation:prep-1:failed');
    list.mockResolvedValue({
      batches: [batch()], indexing: { status: 'running', job_id: 781, error: null },
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(result.current?.stage).toBe('prepare');
    list.mockResolvedValue({
      batches: [batch({ status: 'complete', jobs: [],
        groups: [{ kind: 'ocr', count: 1, status: 'complete' }],
        warnings: ['Page 2 failed during OCR.'] })],
      indexing: { status: 'idle', job_id: null, error: null },
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(result.current).toMatchObject({ tone: 'warning', activityKey: 'preparation:prep-1:failed' });
    await act(async () => { await vi.advanceTimersByTimeAsync(12_000); });
    expect(result.current).toMatchObject({ tone: 'warning', activityKey: 'preparation:prep-1:failed',
      reviewBatchId: 'prep-1', detail: 'Page 2 failed during OCR.' });
  });

  it('retains observed page warnings without resurfacing historical warnings after reconnecting', async () => {
    vi.useFakeTimers();
    const list = vi.spyOn(preparationApi, 'listSearchPreparations').mockResolvedValue({
      batches: [batch()], indexing: { status: 'running', job_id: 781, error: null },
    });
    vi.spyOn(openApi, 'listModelPulls').mockResolvedValue({
      schemaVersion: 'frisket.model_pull_list.v1', pulls: [],
    });
    const { result, unmount } = renderHook(() => useSearchPreparationNotice('project-1', false));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    list.mockResolvedValue({ batches: [batch({ status: 'complete', jobs: [],
      groups: [{ kind: 'ocr', count: 1, status: 'complete' }],
      warnings: ['Page 2 failed during OCR.'] })],
      indexing: { status: 'idle', job_id: null, error: null } });
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(result.current).toMatchObject({ tone: 'warning', title: 'Search prepared with warnings',
      activityKey: 'preparation:prep-1', detail: 'Page 2 failed during OCR.' });
    unmount();
    const reopened = renderHook(() => useSearchPreparationNotice('project-1', false));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(reopened.result.current).toBeNull();
  });

  it('keeps a successful preparation with page warnings visibly distinct from clean completion', () => {
    const warned = batch({ status: 'complete', groups: [{ kind: 'ocr', count: 1, status: 'complete' }],
      jobs: [], warnings: ['Page 2 failed during OCR.'] });
    const notice = deriveSearchPreparationNotice(snapshot({ batches: [warned] }), 'Search index ready');
    expect(notice).toMatchObject({ stage: 'complete', tone: 'warning',
      title: 'Search prepared with warnings', detail: 'Page 2 failed during OCR.', reviewBatchId: warned.id });
    expect(deriveSearchPreparationNotice(snapshot({ batches: [batch(), warned] }))).toMatchObject({ stage: 'prepare', tone: 'active' });
  });

  it('keeps one activity across OCR, indexing and completion and gives a later index its own identity', async () => {
    vi.useFakeTimers();
    const list = vi.spyOn(preparationApi, 'listSearchPreparations').mockResolvedValue({
      schemaVersion: 'frisket.search_preparation_list.v1', batches: [batch()],
      indexing: { status: 'queued', job_id: 781, error: null },
    });
    vi.spyOn(openApi, 'listModelPulls').mockResolvedValue({
      schemaVersion: 'frisket.model_pull_list.v1', pulls: [],
    });
    const { result } = renderHook(() => useSearchPreparationNotice('project-1', false));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current).toMatchObject({ stage: 'prepare', activityKey: 'preparation:prep-1' });

    list.mockResolvedValue({
      schemaVersion: 'frisket.search_preparation_list.v1', batches: [],
      indexing: { status: 'running', job_id: 781, error: null,
        pdf_files: [{ source_id: 'pdf:one', filename: 'Council papers.pdf', completed: 1_896, total: 24_909 }] },
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(result.current).toMatchObject({ stage: 'index', activityKey: 'preparation:prep-1' });
    list.mockResolvedValue({
      schemaVersion: 'frisket.search_preparation_list.v1', batches: [],
      indexing: { status: 'queued', job_id: 782, error: null,
        pdf_files: [{ source_id: 'pdf:one', filename: 'Council papers.pdf', completed: 1_897, total: 24_909 }] },
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(result.current).toMatchObject({ stage: 'index', activityKey: 'preparation:prep-1',
      pdfFiles: [{ source_id: 'pdf:one', completed: 1_897, total: 24_909 }] });
    list.mockResolvedValue({
      schemaVersion: 'frisket.search_preparation_list.v1', batches: [],
      indexing: { status: 'idle', job_id: null, error: null },
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(result.current).toMatchObject({ stage: 'complete', activityKey: 'preparation:prep-1' });

    list.mockResolvedValue({
      schemaVersion: 'frisket.search_preparation_list.v1', batches: [],
      indexing: { status: 'running', job_id: 783, error: null },
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(result.current).toMatchObject({ stage: 'index', activityKey: 'index:783' });
    list.mockResolvedValue({
      schemaVersion: 'frisket.search_preparation_list.v1', batches: [],
      indexing: { status: 'failed', job_id: 783, error: 'Could not index' },
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(result.current).toMatchObject({ stage: 'failed', activityKey: 'index:783:failed' });
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(result.current?.activityKey).toBe('index:783:failed');
  });

  it('shows a durable model setup with determinate byte progress', () => {
    const notice = deriveSearchPreparationNotice(snapshot({
      pulls: [{ scope: 'workspace', pull: pull() }],
    }));
    expect(notice).not.toBeNull();
    render(<SearchPreparationNotice notice={notice!} />);

    const banner = screen.getByTestId('search-preparation-banner');
    expect(banner).toHaveAttribute('data-stage', 'download');
    expect(banner).toHaveTextContent('Downloading EmbeddingGemma · downloading model files');
    expect(banner).toHaveTextContent('Downloading model');
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '42');
  });

  it('uses the authoritative project indexing projection even without a preparation batch', () => {
    const notice = deriveSearchPreparationNotice(snapshot({
      indexing: { status: 'running', job_id: 781, error: null },
    }));
    expect(notice).toMatchObject({ stage: 'index', tone: 'active' });
    render(<SearchPreparationNotice notice={notice!} />);

    expect(screen.getByTestId('search-preparation-banner')).toHaveTextContent(
      'Indexing search content',
    );
    fireEvent.click(screen.getByTestId('search-preparation-details'));
    expect(screen.getByText('Job 781')).toBeInTheDocument();
  });

  it('shows active OCR ahead of indexing that is only queued behind it', () => {
    const notice = deriveSearchPreparationNotice(snapshot({
      batches: [batch()],
      indexing: { status: 'queued', job_id: 781, error: null },
    }));

    expect(notice).toMatchObject({
      activityKey: 'preparation:prep-1',
      stage: 'prepare',
      detail: 'Reading scanned PDFs · 2 files',
    });
  });

  it('surfaces failed OCR rows while the remaining files continue', () => {
    const notice = deriveSearchPreparationNotice(snapshot({
      batches: [batch({
        jobs: [{
          job_id: 473,
          action_kind: 'ocr',
          status: 'running',
          progress: preparationProgress({
            row_errors: {
              total_failed_rows: 1,
              groups: [{
                code: 'image_result_invalid',
                count: 1,
                message: 'One PDF page could not be recognized.',
                row_ids: [14],
                terminal: true,
              }],
            },
          }),
        }],
      })],
      indexing: { status: 'queued', job_id: 474, error: null },
    }));

    expect(notice).toMatchObject({
      activityKey: 'preparation:prep-1:failed',
      stage: 'failed',
      tone: 'error',
      detail: '1 of 3 files failed · 2/3 processed',
      progress: 67,
    });
    render(<SearchPreparationNotice notice={notice!} />);
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '67');
    fireEvent.click(screen.getByTestId('search-preparation-details'));
    expect(screen.getByText('One PDF page could not be recognized.')).toBeInTheDocument();
    expect(screen.getByText('Reading scanned PDFs · 2 files')).toBeInTheDocument();
    expect(screen.getByText('Search indexing queued')).toBeInTheDocument();
  });

  it('keeps required approval actionable ahead of background indexing', () => {
    const onReviewPreparation = vi.fn();
    const notice = deriveSearchPreparationNotice(snapshot({
      batches: [batch({ status: 'needs_consent' })],
      indexing: { status: 'running', job_id: 781, error: null },
    }));
    render(<SearchPreparationNotice notice={notice!} onReviewPreparation={onReviewPreparation} />);

    const banner = screen.getByTestId('search-preparation-banner');
    expect(banner).toHaveAttribute('data-stage', 'approval');
    fireEvent.click(screen.getByTestId('search-preparation-review'));
    expect(onReviewPreparation).toHaveBeenCalledWith('prep-1');
  });

  it('keeps a preparation failure ahead of another active batch and reports concurrent indexing in details', () => {
    const notice = deriveSearchPreparationNotice(snapshot({
      batches: [
        batch({ id: 'still-running', status: 'preparing' }),
        batch({ id: 'ocr-failed', status: 'failed', errors: ['OCR could not read row 1 of 3.'] }),
      ],
      indexing: { status: 'running', job_id: 781, error: null },
    }));
    render(<SearchPreparationNotice notice={notice!} />);

    const banner = screen.getByTestId('search-preparation-banner');
    expect(banner).toHaveAttribute('data-stage', 'failed');
    expect(banner).toHaveTextContent('OCR could not read row 1 of 3.');
    fireEvent.click(screen.getByTestId('search-preparation-details'));
    expect(screen.getByText('Indexing search content')).toBeInTheDocument();
  });

  it('keeps the latest failed setup visible with its backend error message', () => {
    const notice = deriveSearchPreparationNotice(snapshot({
      pulls: [{
        scope: 'workspace',
        pull: pull({
          status: 'failed',
          error: { code: 'download_failed', message: 'The model archive checksum did not match.' },
          finished_at: '2026-10-16T12:02:00Z',
        }),
      }],
    }));
    render(<SearchPreparationNotice notice={notice!} />);

    const banner = screen.getByTestId('search-preparation-banner');
    expect(banner).toHaveAttribute('data-stage', 'failed');
    expect(banner).toHaveAttribute('role', 'alert');
    expect(banner).toHaveTextContent('The model archive checksum did not match.');
  });

  it('keeps an observed model failure ahead of indexing work that it blocks', () => {
    const notice = deriveSearchPreparationNotice(snapshot({
      indexing: { status: 'pending', job_id: null, error: null },
      pulls: [{
        scope: 'workspace',
        pull: pull({
          status: 'failed',
          error: { code: 'download_failed', message: 'Model transport failed.' },
          finished_at: '2026-10-16T12:02:00Z',
        }),
      }],
    }));

    expect(notice).toMatchObject({
      stage: 'failed',
      title: 'Model setup needs attention',
      detail: 'Model transport failed.',
    });
  });

  it('shows the runtime remedy for a preparation that could not start', () => {
    const notice = deriveSearchPreparationNotice(snapshot({
      batches: [batch({
        status: 'failed',
        groups: [{ kind: 'transcribe', count: 2, status: 'not_prepared' }],
        models: [{
          setup_ref: 'parakeet', display_name: 'Parakeet', purpose: 'transcribe',
          required: true, ready: false, runtime_ready: false, can_setup: false,
          remediation: 'Install the local audio runtime, then prepare these files again.',
          download_bytes: null, additional_runtime_download: false,
          license: null, source_url: null, pull_id: null,
        }],
      })],
    }));
    expect(notice?.detail).toContain('Install the local audio runtime');
    expect(notice?.title).toBe('Some files need local setup');
  });

  it('restores an unavailable runtime explanation after reopening the project', async () => {
    vi.spyOn(preparationApi, 'listSearchPreparations').mockResolvedValue({
      schemaVersion: 'frisket.search_preparation_list.v1',
      batches: [batch({
        status: 'failed',
        models: [{
          setup_ref: 'parakeet', display_name: 'Parakeet', purpose: 'transcribe',
          required: true, ready: false, runtime_ready: false, can_setup: false,
          remediation: 'Install the local audio runtime.', download_bytes: null,
          additional_runtime_download: false, license: null, source_url: null, pull_id: null,
        }],
      })],
      indexing: { status: 'idle', job_id: null, error: null },
    });
    vi.spyOn(openApi, 'listModelPulls').mockResolvedValue({
      schemaVersion: 'frisket.model_pull_list.v1', pulls: [],
    });
    const { result } = renderHook(() => useSearchPreparationNotice('project-1', false));

    await waitFor(() => expect(result.current?.detail).toContain('Install the local audio runtime.'));
  });

  it('does not resurface a historical failure during unrelated pending indexing', async () => {
    vi.spyOn(preparationApi, 'listSearchPreparations').mockResolvedValue({
      schemaVersion: 'frisket.search_preparation_list.v1',
      batches: [batch({
        id: 'just-failed',
        status: 'failed',
        groups: [{ kind: 'ocr', count: 3, status: 'failed' }],
        errors: ['OCR could not read the imported PDFs.'],
      })],
      indexing: { status: 'pending', job_id: null, error: null },
    });
    vi.spyOn(openApi, 'listModelPulls').mockResolvedValue({
      schemaVersion: 'frisket.model_pull_list.v1', pulls: [],
    });

    const { result } = renderHook(() => useSearchPreparationNotice('project-1', false));

    await waitFor(() => expect(result.current).toMatchObject({
      stage: 'index',
      activityKey: 'index:pending',
    }));
  });

  it('does not attach historical warnings to unrelated indexing after reload', async () => {
    vi.useFakeTimers();
    const historical = batch({ status: 'complete', jobs: [],
      groups: [{ kind: 'ocr', count: 1, status: 'complete' }],
      warnings: ['An old PDF page failed.'] });
    const list = vi.spyOn(preparationApi, 'listSearchPreparations').mockResolvedValue({
      batches: [historical], indexing: { status: 'running', job_id: 900, error: null },
    });
    vi.spyOn(openApi, 'listModelPulls').mockResolvedValue({
      schemaVersion: 'frisket.model_pull_list.v1', pulls: [],
    });
    const { result } = renderHook(() => useSearchPreparationNotice('project-1', false));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current).toMatchObject({ stage: 'index', activityKey: 'index:900' });
    list.mockResolvedValue({ batches: [historical], indexing: { status: 'idle', job_id: null, error: null } });
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(result.current).toMatchObject({ tone: 'complete', activityKey: 'index:900', reviewBatchId: null });
    expect(result.current?.details).not.toContain('An old PDF page failed.');
    await act(async () => { await vi.advanceTimersByTimeAsync(12_000); });
    expect(result.current).toBeNull();
  });

  it('keeps a failed project index visible with its exact job error', () => {
    const notice = deriveSearchPreparationNotice(snapshot({
      indexing: {
        status: 'failed',
        job_id: 782,
        error: 'Search index retries were exhausted.',
      },
    }));
    render(<SearchPreparationNotice notice={notice!} />);

    const banner = screen.getByTestId('search-preparation-banner');
    expect(banner).toHaveAttribute('data-stage', 'failed');
    expect(banner).toHaveTextContent('Search index retries were exhausted.');
  });

  it('reports the media preparation stage without inventing file progress from job counts', () => {
    const notice = deriveSearchPreparationNotice(snapshot({
      batches: [batch({
        groups: [{ kind: 'transcribe', count: 3, status: 'running' }],
        jobs: [
          { job_id: 1, action_kind: 'audio.transcribe', status: 'done' },
          { job_id: 2, action_kind: 'audio.transcribe', status: 'running' },
        ],
      })],
    }));
    render(<SearchPreparationNotice notice={notice!} />);

    expect(screen.getByTestId('search-preparation-banner')).toHaveTextContent('Transcribing media · 3 files');
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });

  it('labels completion as prepared content becoming searchable', () => {
    const notice = deriveSearchPreparationNotice(snapshot(), 'Search index ready');
    render(<SearchPreparationNotice notice={notice!} />);

    const banner = screen.getByTestId('search-preparation-banner');
    expect(banner).toHaveAttribute('data-stage', 'complete');
    expect(banner).toHaveTextContent('Search index ready');
    expect(banner).toHaveTextContent('Search indexing finished.');
  });
});
