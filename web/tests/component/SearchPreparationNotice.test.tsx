// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ModelPullDto } from '../../src/api/types';
import type { SearchPreparationBatch } from '../../src/api/searchPreparation';
import { SearchPreparationNotice } from '../../src/components/SearchPreparationNotice';
import {
  deriveSearchPreparationNotice,
  type SearchPreparationSnapshot,
} from '../../src/components/useSearchPreparationNotice';

afterEach(cleanup);

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

function snapshot(overrides: Partial<SearchPreparationSnapshot> = {}): SearchPreparationSnapshot {
  return {
    batches: [],
    indexing: { status: 'idle', job_id: null, error: null },
    pulls: [],
    ...overrides,
  };
}

describe('search preparation notice projection', () => {
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

  it('keeps required approval actionable ahead of background indexing', () => {
    const onReviewApproval = vi.fn();
    const notice = deriveSearchPreparationNotice(snapshot({
      batches: [batch({ status: 'needs_consent' })],
      indexing: { status: 'running', job_id: 781, error: null },
    }));
    render(<SearchPreparationNotice notice={notice!} onReviewApproval={onReviewApproval} />);

    const banner = screen.getByTestId('search-preparation-banner');
    expect(banner).toHaveAttribute('data-stage', 'approval');
    fireEvent.click(screen.getByTestId('search-preparation-review'));
    expect(onReviewApproval).toHaveBeenCalledOnce();
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

  it('reports the actual media preparation stage and job progress', () => {
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
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '50');
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
