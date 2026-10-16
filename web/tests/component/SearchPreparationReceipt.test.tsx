// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { SearchPreparationBatch } from '../../src/api/searchPreparation';

const {
  getSearchPreparation,
  decideSearchPreparation,
  stopSearchPreparation,
  createSearchPreparation,
  getSearchPreparationTextSamples,
} = vi.hoisted(() => ({
  getSearchPreparation: vi.fn(),
  decideSearchPreparation: vi.fn(),
  stopSearchPreparation: vi.fn(),
  createSearchPreparation: vi.fn(),
  getSearchPreparationTextSamples: vi.fn(),
}));

vi.mock('../../src/api/searchPreparation', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/searchPreparation')>(),
  getSearchPreparation,
  decideSearchPreparation,
  stopSearchPreparation,
  createSearchPreparation,
  getSearchPreparationTextSamples,
}));

vi.mock('../../src/api/open', () => ({
  getModelPull: vi.fn(),
}));

import { SearchPreparationReceipt } from '../../src/components/SearchPreparationReceipt';

const consentBatch: SearchPreparationBatch = {
  schemaVersion: 'frisket.search_preparation.v1',
  id: 'prep-1',
  sheet_id: 7,
  plan_hash: 'sha256:plan',
  status: 'needs_consent',
  groups: [
    { kind: 'native_pdf', count: 1, status: 'complete' },
    { kind: 'ocr', count: 2, status: 'needs_consent' },
  ],
  models: [
    {
      setup_ref: 'engine-setup:rapidocr-default.local@1',
      display_name: 'RapidOCR',
      purpose: 'read_text',
      required: true,
      ready: false,
      runtime_ready: true,
      download_bytes: 16_631_306,
      additional_runtime_download: false,
      remediation: null,
      pull_id: null,
    },
    {
      setup_ref: 'engine-setup:embeddinggemma-2.local@1',
      display_name: 'EmbeddingGemma',
      purpose: 'search_by_meaning',
      required: false,
      ready: false,
      runtime_ready: true,
      download_bytes: 1_525_784_180,
      additional_runtime_download: true,
      remediation: null,
      pull_id: null,
    },
  ],
  jobs: [],
  can_stop: false,
  can_setup: true,
  errors: [],
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('SearchPreparationReceipt', () => {
  it('does not authorize downloads on render and keeps semantic search optional', async () => {
    getSearchPreparation.mockResolvedValue(consentBatch);
    decideSearchPreparation.mockResolvedValue({ ...consentBatch, status: 'preparing' });

    render(
      <SearchPreparationReceipt
        projectId="project-1"
        batchId="prep-1"
        onDone={vi.fn()}
        onError={vi.fn()}
      />,
    );

    const semantic = await screen.findByTestId('search-preparation-semantic-model');
    expect(decideSearchPreparation).not.toHaveBeenCalled();
    expect((semantic as HTMLInputElement).checked).toBe(false);
    expect((screen.getByTestId('search-preparation-model-read_text').querySelector('input') as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText('1.4 GB')).not.toBeNull();
    expect(screen.getByText(/Additional runtime files may also be downloaded/)).not.toBeNull();

    fireEvent.click(screen.getByTestId('search-preparation-decision-prepare'));
    await waitFor(() => expect(decideSearchPreparation).toHaveBeenCalledWith(
      'project-1',
      'prep-1',
      {
        plan_hash: 'sha256:plan',
        decision: 'prepare',
        selected_setup_refs: ['engine-setup:rapidocr-default.local@1'],
      },
    ));
  });

  it('declines durably without setup refs', async () => {
    getSearchPreparation.mockResolvedValue(consentBatch);
    decideSearchPreparation.mockResolvedValue({
      ...consentBatch,
      status: 'complete',
      groups: consentBatch.groups.map((group) => (
        group.kind === 'ocr' ? { ...group, status: 'not_prepared' as const } : group
      )),
    });

    render(
      <SearchPreparationReceipt
        projectId="project-1"
        batchId="prep-1"
        onDone={vi.fn()}
        onError={vi.fn()}
      />,
    );
    fireEvent.click(await screen.findByTestId('search-preparation-decision-not-now'));
    await waitFor(() => expect(decideSearchPreparation).toHaveBeenCalledWith(
      'project-1',
      'prep-1',
      { plan_hash: 'sha256:plan', decision: 'not_now', selected_setup_refs: [] },
    ));
  });

  it('forces OCR only after the explicit text-check action', async () => {
    getSearchPreparation.mockResolvedValue({ ...consentBatch, status: 'complete' });
    getSearchPreparationTextSamples.mockResolvedValue({
      schemaVersion: 'frisket.search_preparation_text_samples.v1',
      batch_id: 'prep-1',
      samples: [{
        row_id: 42,
        column_id: 5,
        filename: 'native.pdf',
        page_number: 1,
        page_count: 2,
        preview_url: '/api/projects/project-1/blobs/hash/pages/1/image',
        text: 'Native text from the PDF',
        source: 'native',
        truncated: false,
      }],
    });
    createSearchPreparation.mockResolvedValue({ ...consentBatch, id: 'prep-force' });

    render(
      <SearchPreparationReceipt
        projectId="project-1"
        batchId="prep-1"
        onDone={vi.fn()}
        onError={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByTestId('search-preparation-check-text'));
    expect(createSearchPreparation).not.toHaveBeenCalled();
    await screen.findByText('Native text from the PDF');
    expect(screen.getByText(/does not change the original PDF/)).not.toBeNull();
    fireEvent.click(screen.getByTestId('search-preparation-force-ocr'));
    await waitFor(() => expect(createSearchPreparation).toHaveBeenCalledWith('project-1', {
      sheet_id: 7,
      row_ids: [42],
      force_ocr: true,
    }));
  });
});
