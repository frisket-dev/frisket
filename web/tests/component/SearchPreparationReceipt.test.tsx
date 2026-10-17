// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { SearchPreparationBatch } from '../../src/api/searchPreparation';

const {
  getSearchPreparation,
  decideSearchPreparation,
  stopSearchPreparation,
  createSearchPreparation,
  getSearchPreparationTextSamples,
  getModelPull,
} = vi.hoisted(() => ({
  getSearchPreparation: vi.fn(),
  decideSearchPreparation: vi.fn(),
  stopSearchPreparation: vi.fn(),
  createSearchPreparation: vi.fn(),
  getSearchPreparationTextSamples: vi.fn(),
  getModelPull: vi.fn(),
}));

vi.mock('../../src/api/searchPreparation', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/searchPreparation')>(),
  getSearchPreparation,
  decideSearchPreparation,
  stopSearchPreparation,
  createSearchPreparation,
  getSearchPreparationTextSamples,
}));

vi.mock('../../src/api/open', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/open')>(),
  getModelPull,
}));

import { SearchPreparationReceipt } from '../../src/components/SearchPreparationReceipt';

function deferred<T>(): { promise: Promise<T>; resolve(value: T): void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

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
      can_setup: true,
      download_bytes: 16_631_306,
      additional_runtime_download: false,
      remediation: null,
      pull_id: null,
      license: 'Apache-2.0',
      source_url: 'https://example.test/rapidocr',
    },
    {
      setup_ref: 'engine-setup:embeddinggemma-2.local@1',
      display_name: 'EmbeddingGemma',
      purpose: 'search_by_meaning',
      required: false,
      ready: false,
      runtime_ready: true,
      can_setup: true,
      download_bytes: 1_525_784_180,
      additional_runtime_download: true,
      remediation: 'The explicit setup also installs additional runtime components.',
      pull_id: null,
      license: 'Gemma',
      source_url: 'https://example.test/embeddinggemma',
    },
  ],
  jobs: [],
  can_stop: false,
  can_setup: true,
  errors: [],
};

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

beforeEach(() => {
  getSearchPreparationTextSamples.mockResolvedValue({
    schemaVersion: 'frisket.search_preparation_text_samples.v1',
    batch_id: 'prep-1',
    samples: [],
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('SearchPreparationReceipt', () => {
  it('shows partial page warnings without claiming all text is ready', async () => {
    getSearchPreparation.mockResolvedValue({
      ...consentBatch, status: 'complete',
      groups: [{ kind: 'ocr', count: 1, status: 'complete' }],
      warnings: ['Page 2 failed during OCR.'],
    });
    render(<SearchPreparationReceipt projectId="project-1" batchId="prep-1"
      onDone={vi.fn()} onError={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Text prepared with warnings' })).toBeTruthy());
    expect(screen.getByRole('note', { name: 'Preparation warnings' }).textContent).toContain('Page 2 failed during OCR.');
    expect(screen.queryByRole('heading', { name: 'Text ready for keyword search' })).toBeNull();
  });

  it('shows processed and failed counts from the live OCR action', async () => {
    getSearchPreparation.mockResolvedValue({
      ...consentBatch,
      status: 'preparing',
      groups: [{ kind: 'ocr', count: 3, status: 'running' }],
      jobs: [{
        job_id: 473,
        action_kind: 'ocr',
        status: 'running',
        progress: preparationProgress(),
      }],
    });
    render(<SearchPreparationReceipt projectId="project-1" batchId="prep-1" onDone={vi.fn()} onError={vi.fn()} />);

    const group = await screen.findByTestId('search-preparation-group-ocr');
    expect(group.textContent).toContain('Running OCR');
    expect(group.textContent).toContain('2/3 processed · 1 failed');
  });

  it('keeps an empty planning response visibly active instead of calling it ready', async () => {
    getSearchPreparation.mockResolvedValue({
      ...consentBatch,
      status: 'planning',
      groups: [],
      models: [],
      jobs: [],
    });

    render(
      <SearchPreparationReceipt
        projectId="project-1"
        batchId="prep-1"
        onDone={vi.fn()}
        onError={vi.fn()}
      />,
    );

    expect(await screen.findByRole('heading', { name: 'Planning search preparation' })).not.toBeNull();
    expect(screen.getByRole('status').textContent).toContain('Checking files and planning preparation');
    expect(screen.getByTestId('search-preparation-done').textContent).toContain('Close');
    expect(screen.queryByText('Ready to search')).toBeNull();
  });

  it('explains an unavailable required runtime outside download consent', async () => {
    getSearchPreparation.mockResolvedValue({
      ...consentBatch,
      status: 'failed',
      groups: [{ kind: 'transcribe', count: 2, status: 'not_prepared' }],
      models: [{
        ...consentBatch.models[0], display_name: 'Parakeet', purpose: 'transcribe',
        runtime_ready: false, can_setup: false,
        remediation: 'Install the standard local audio runtime, then prepare these files again.',
      }],
      can_setup: false,
    });
    render(<SearchPreparationReceipt projectId="project-1" batchId="prep-1" onDone={vi.fn()} onError={vi.fn()} />);

    expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Parakeet'));
    expect(screen.getByRole('alert').textContent).toContain('Install the standard local audio runtime');
    expect(screen.queryByTestId('search-preparation-model-consent')).toBeNull();
  });

  it('describes completed file text separately from semantic search readiness', async () => {
    getSearchPreparation.mockResolvedValue({
      ...consentBatch, status: 'complete',
      groups: [{ kind: 'native_pdf', count: 1, status: 'complete' }],
    });
    render(<SearchPreparationReceipt projectId="project-1" batchId="prep-1" onDone={vi.fn()} onError={vi.fn()} />);

    expect(await screen.findByRole('heading', { name: 'Text ready for keyword search' })).not.toBeNull();
  });

  it('refreshes a visible preview on batch completion without repeatedly reading settled samples', async () => {
    vi.useFakeTimers();
    getSearchPreparation
      .mockResolvedValueOnce({ ...consentBatch, status: 'preparing' })
      .mockResolvedValue({ ...consentBatch, status: 'complete' });
    const sample = {
      row_id: 42, column_id: 5, filename: 'bad-layer.pdf', page_number: 1,
      page_count: 1, page_scoped: true, preview_url: '/preview',
      text: 'Garbled original text', source: 'prepared', truncated: false,
    };
    const refresh = deferred<Awaited<ReturnType<typeof getSearchPreparationTextSamples>>>();
    getSearchPreparationTextSamples
      .mockResolvedValueOnce({ batch_id: 'prep-1', samples: [sample] })
      .mockReturnValue(refresh.promise);
    render(<SearchPreparationReceipt projectId="project-1" batchId="prep-1" onDone={vi.fn()} onError={vi.fn()} />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText('Garbled original text')).not.toBeNull();

    expect(getSearchPreparationTextSamples).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
    expect(getSearchPreparationTextSamples).toHaveBeenCalledTimes(2);
    expect(screen.getByText('Garbled original text')).not.toBeNull();
    await act(async () => {
      refresh.resolve({ batch_id: 'prep-1', samples: [{ ...sample, text: 'Correct replacement text' }] });
    });
    expect(screen.getByText('Correct replacement text')).not.toBeNull();
    expect(screen.queryByText('Garbled original text')).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(getSearchPreparationTextSamples).toHaveBeenCalledTimes(2);
  });

  it('keeps a dismissed text review closed when the batch completes', async () => {
    vi.useFakeTimers();
    getSearchPreparation
      .mockResolvedValueOnce({ ...consentBatch, status: 'preparing' })
      .mockResolvedValue({ ...consentBatch, status: 'complete' });
    getSearchPreparationTextSamples.mockResolvedValue({
      batch_id: 'prep-1', samples: [{
        row_id: 42, column_id: 5, filename: 'native.pdf', page_number: 1,
        page_count: 1, page_scoped: true, preview_url: '/preview',
        text: 'Correct native text', source: 'native', truncated: false,
      }],
    });
    render(<SearchPreparationReceipt projectId="project-1" batchId="prep-1" onDone={vi.fn()} onError={vi.fn()} />);
    await act(async () => { await Promise.resolve(); });
    fireEvent.click(screen.getByTestId('search-preparation-keep-text'));

    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(screen.queryByTestId('search-preparation-text-preview')).toBeNull();
    expect(getSearchPreparationTextSamples).toHaveBeenCalledTimes(1);
  });

  it('checks semantic search by default without authorizing downloads and allows opting out', async () => {
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
    expect((semantic as HTMLInputElement).checked).toBe(true);
    expect((screen.getByTestId('search-preparation-model-read_text').querySelector('input') as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText('About 1.4 GB')).not.toBeNull();
    expect(screen.queryByText(/Nothing downloads until you continue/)).toBeNull();
    expect(screen.queryByText(/Additional runtime files may also be downloaded/)).toBeNull();
    expect(screen.queryByText(/installs additional runtime components/)).toBeNull();
    expect(screen.queryByRole('link', { name: 'Source' })).toBeNull();
    const consentFooter = screen.getByTestId('search-preparation-decision-not-now').closest('footer');
    expect(consentFooter).not.toBeNull();
    expect(Array.from(consentFooter!.querySelectorAll('button'), (button) => button.textContent)).toEqual([
      'Not now',
      'Download and prepare',
    ]);
    expect(screen.queryByTestId('search-preparation-done')).toBeNull();

    fireEvent.click(semantic);
    expect((semantic as HTMLInputElement).checked).toBe(false);
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

  it('preserves the semantic model selection when the decision request fails', async () => {
    const onError = vi.fn();
    getSearchPreparation.mockResolvedValue(consentBatch);
    decideSearchPreparation.mockRejectedValue(new Error('temporary outage'));

    render(
      <SearchPreparationReceipt
        projectId="project-1"
        batchId="prep-1"
        onDone={vi.fn()}
        onError={onError}
      />,
    );

    const semantic = await screen.findByTestId('search-preparation-semantic-model');
    expect((semantic as HTMLInputElement).checked).toBe(true);
    fireEvent.click(screen.getByTestId('search-preparation-decision-prepare'));

    await waitFor(() => expect(onError).toHaveBeenCalledWith('temporary outage'));
    expect((semantic as HTMLInputElement).checked).toBe(true);
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

  it('does not let an older poll response overwrite a preparation decision', async () => {
    vi.useFakeTimers();
    const stalePoll = deferred<SearchPreparationBatch>();
    getSearchPreparation
      .mockResolvedValueOnce(consentBatch)
      .mockReturnValueOnce(stalePoll.promise);
    decideSearchPreparation.mockResolvedValue({
      ...consentBatch,
      status: 'preparing',
      groups: consentBatch.groups.map((group) => (
        group.kind === 'ocr' ? { ...group, status: 'queued' as const } : group
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
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTestId('search-preparation-decision-prepare')).not.toBeNull();

    await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
    expect(getSearchPreparation).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByTestId('search-preparation-decision-prepare'));
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByTestId('search-preparation-model-consent')).toBeNull();

    await act(async () => { stalePoll.resolve(consentBatch); });
    expect(screen.queryByTestId('search-preparation-model-consent')).toBeNull();
  });

  it('does not let an older poll response overwrite a stop result', async () => {
    vi.useFakeTimers();
    const stalePoll = deferred<SearchPreparationBatch>();
    const preparingBatch: SearchPreparationBatch = {
      ...consentBatch,
      status: 'preparing',
      can_stop: true,
      groups: consentBatch.groups.map((group) => (
        group.kind === 'ocr' ? { ...group, status: 'running' as const } : group
      )),
    };
    getSearchPreparation
      .mockResolvedValueOnce(preparingBatch)
      .mockReturnValueOnce(stalePoll.promise);
    stopSearchPreparation.mockResolvedValue({
      ...preparingBatch,
      status: 'stopped',
      can_stop: false,
      groups: preparingBatch.groups.map((group) => (
        group.kind === 'ocr' ? { ...group, status: 'stopped' as const } : group
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
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTestId('search-preparation-stop')).not.toBeNull();
    expect(screen.getByTestId('search-preparation-done')).not.toBeNull();

    await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
    expect(getSearchPreparation).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByTestId('search-preparation-stop'));
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByTestId('search-preparation-stop')).toBeNull();
    expect(screen.getByText('Stopped')).not.toBeNull();

    await act(async () => { stalePoll.resolve(preparingBatch); });
    expect(screen.queryByTestId('search-preparation-stop')).toBeNull();
    expect(screen.getByText('Stopped')).not.toBeNull();
  });

  it('prepares without download wording when every required model is ready', async () => {
    const readyBatch = {
      ...consentBatch,
      can_setup: false,
      models: consentBatch.models.map((model) => ({ ...model, ready: true, can_setup: false })),
    } satisfies SearchPreparationBatch;
    getSearchPreparation.mockResolvedValue(readyBatch);
    decideSearchPreparation.mockResolvedValue({ ...readyBatch, status: 'preparing' });

    render(
      <SearchPreparationReceipt
        projectId="project-1"
        batchId="prep-1"
        onDone={vi.fn()}
        onError={vi.fn()}
      />,
    );

    expect(await screen.findByText('Extract text with OCR?')).not.toBeNull();
    expect(screen.queryByText('Download local models?')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Extract text with OCR' }));
    await waitFor(() => expect(decideSearchPreparation).toHaveBeenCalledWith(
      'project-1',
      'prep-1',
      { plan_hash: 'sha256:plan', decision: 'prepare', selected_setup_refs: [] },
    ));
  });

  it('shows a scanned PDF before OCR and keeps the explicit extraction decision enabled when OCR is installed', async () => {
    const readyBatch = {
      ...consentBatch,
      can_setup: false,
      groups: [{ kind: 'ocr', count: 1, status: 'needs_consent' as const }],
      models: consentBatch.models.map((model) => ({ ...model, ready: true, can_setup: false })),
    } satisfies SearchPreparationBatch;
    getSearchPreparation.mockResolvedValue(readyBatch);
    getSearchPreparationTextSamples.mockResolvedValue({
      schemaVersion: 'frisket.search_preparation_text_samples.v1',
      batch_id: 'prep-1',
      samples: [{
        row_id: 42,
        column_id: 5,
        filename: 'scan.pdf',
        page_number: 1,
        page_count: 3,
        page_scoped: false,
        preview_url: '/api/projects/project-1/blobs/hash/pages/1/image',
        text: '',
        source: 'native',
        truncated: false,
      }],
    });
    decideSearchPreparation.mockResolvedValue({ ...readyBatch, status: 'preparing' });

    render(
      <SearchPreparationReceipt
        projectId="project-1"
        batchId="prep-1"
        onDone={vi.fn()}
        onError={vi.fn()}
      />,
    );

    expect(await screen.findByText('No embedded text found')).not.toBeNull();
    expect(screen.getByAltText('Page 1').getAttribute('src')).toBe(
      '/api/projects/project-1/blobs/hash/pages/1/image',
    );
    expect(screen.getByText('This page needs OCR before Frisket can read it.')).not.toBeNull();
    const extract = screen.getByRole('button', { name: 'Extract text with OCR' });
    expect((extract as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(extract);
    await waitFor(() => expect(decideSearchPreparation).toHaveBeenCalledWith(
      'project-1',
      'prep-1',
      { plan_hash: 'sha256:plan', decision: 'prepare', selected_setup_refs: [] },
    ));
  });

  it('uses download wording and includes the optional model by default', async () => {
    const optionalOnlyBatch = {
      ...consentBatch,
      models: consentBatch.models.map((model) => (
        model.required ? { ...model, ready: true, can_setup: false } : model
      )),
    } satisfies SearchPreparationBatch;
    getSearchPreparation.mockResolvedValue(optionalOnlyBatch);
    decideSearchPreparation.mockResolvedValue({ ...optionalOnlyBatch, status: 'preparing' });

    render(
      <SearchPreparationReceipt
        projectId="project-1"
        batchId="prep-1"
        onDone={vi.fn()}
        onError={vi.fn()}
      />,
    );

    const semantic = await screen.findByTestId('search-preparation-semantic-model');
    expect((semantic as HTMLInputElement).checked).toBe(true);
    expect(screen.getByRole('button', { name: 'Download and prepare' })).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Download and prepare' }));
    await waitFor(() => expect(decideSearchPreparation).toHaveBeenCalledWith(
      'project-1',
      'prep-1',
      {
        plan_hash: 'sha256:plan',
        decision: 'prepare',
        selected_setup_refs: ['engine-setup:embeddinggemma-2.local@1'],
      },
    ));
  });

  it('keeps unsupported required models visible without authorizing their setup', async () => {
    const unsupported = {
      ...consentBatch.models[0],
      can_setup: false,
      runtime_ready: false,
      remediation: 'Install the local transcription runtime first.',
    };
    const installable = {
      ...consentBatch.models[1],
      required: true,
      can_setup: true,
    };
    const unsupportedOptional = {
      ...consentBatch.models[1],
      setup_ref: 'engine-setup:optional-unsupported.local@1',
      can_setup: false,
      remediation: 'Optional semantic setup is unavailable on this runtime.',
    };
    const mixedBatch = {
      ...consentBatch,
      models: [unsupported, installable, unsupportedOptional],
      can_setup: true,
    } satisfies SearchPreparationBatch;
    getSearchPreparation.mockResolvedValue(mixedBatch);
    decideSearchPreparation.mockResolvedValue({ ...mixedBatch, status: 'preparing' });

    render(
      <SearchPreparationReceipt
        projectId="project-1"
        batchId="prep-1"
        onDone={vi.fn()}
        onError={vi.fn()}
      />,
    );

    const unsupportedRow = await screen.findByTestId('search-preparation-model-read_text');
    const unsupportedCheckbox = unsupportedRow.querySelector('input') as HTMLInputElement;
    expect(unsupportedCheckbox.disabled).toBe(true);
    expect(unsupportedCheckbox.checked).toBe(false);
    expect(unsupportedRow.textContent).toContain('Install the local transcription runtime first.');
    const optionalCheckbox = screen.getByTestId('search-preparation-semantic-model') as HTMLInputElement;
    expect(optionalCheckbox.disabled).toBe(true);
    expect(optionalCheckbox.checked).toBe(false);

    fireEvent.click(screen.getByTestId('search-preparation-decision-prepare'));
    await waitFor(() => expect(decideSearchPreparation).toHaveBeenCalledWith(
      'project-1',
      'prep-1',
      {
        plan_hash: 'sha256:plan',
        decision: 'prepare',
        selected_setup_refs: ['engine-setup:embeddinggemma-2.local@1'],
      },
    ));
  });

  it('shows native text automatically and starts replacement OCR only after the explicit choice', async () => {
    getSearchPreparation.mockResolvedValue({ ...consentBatch, status: 'complete' });
    getSearchPreparationTextSamples.mockResolvedValue({
      schemaVersion: 'frisket.search_preparation_text_samples.v1',
      batch_id: 'prep-1',
      samples: [{
        row_id: 42,
        column_id: 5,
        filename: 'native.pdf',
        page_number: 3,
        page_count: 5,
        page_scoped: true,
        preview_url: '/api/projects/project-1/blobs/hash/pages/3/image',
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

    expect(createSearchPreparation).not.toHaveBeenCalled();
    await screen.findByText('Native text from the PDF');
    expect(screen.getByText('Does this text look right?')).not.toBeNull();
    expect(screen.getByRole('button', { name: 'Yes, keep it' })).not.toBeNull();
    expect(screen.getByText('Page 3 of 5')).not.toBeNull();
    expect(screen.queryByRole('button', { name: 'Page 1' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Refresh sample pages' })).toBeNull();
    expect(screen.getByText(/Your PDF stays unchanged/)).not.toBeNull();
    fireEvent.click(screen.getByTestId('search-preparation-force-ocr'));
    await waitFor(() => expect(createSearchPreparation).toHaveBeenCalledWith('project-1', {
      sheet_id: 7,
      row_ids: [42],
      force_ocr: true,
    }));
  });

  it('reviews the visible prepared native PDF independently of another document requiring OCR', async () => {
    getSearchPreparation.mockResolvedValue({
      ...consentBatch, status: 'complete',
      groups: [{ kind: 'native_pdf', count: 1, status: 'complete' }, { kind: 'ocr', count: 1, status: 'complete' }],
    });
    const sample = {
      row_id: 42, column_id: 5, filename: 'native.pdf', page_number: 1,
      page_count: 1, page_scoped: true, preview_url: '/native-preview',
      text: 'Native PDF text stored as prepared content', source: 'prepared', truncated: false,
    };
    getSearchPreparationTextSamples.mockResolvedValue({
      batch_id: 'prep-1', samples: [sample, { ...sample, row_id: 43, filename: 'scanned.pdf', text: 'Other document OCR text' }],
    });
    const replacement = deferred<SearchPreparationBatch>();
    createSearchPreparation.mockReturnValue(replacement.promise);
    render(<SearchPreparationReceipt projectId="project-1" batchId="prep-1" onDone={vi.fn()} onError={vi.fn()} />);

    await screen.findByText(sample.text);
    expect(screen.getByText('Does this text look right?')).not.toBeNull();
    expect(screen.getByText('Extracted text')).not.toBeNull();
    expect(screen.queryByText('Check the OCR text')).toBeNull();
    expect(screen.getByTestId('search-preparation-keep-text')).not.toBeNull();
    fireEvent.click(screen.getByTestId('search-preparation-force-ocr'));
    expect(createSearchPreparation).toHaveBeenCalledWith('project-1', {
      sheet_id: 7, row_ids: [42], force_ocr: true,
    });
    expect((screen.getByTestId('search-preparation-force-ocr') as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId('search-preparation-keep-text') as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { replacement.resolve({ ...consentBatch, id: 'prep-replace-native' }); });
  });

  it('does not overlap a slow automatic text sample read', async () => {
    vi.useFakeTimers();
    const slowSample = deferred<Awaited<ReturnType<typeof getSearchPreparationTextSamples>>>();
    getSearchPreparation.mockResolvedValue({ ...consentBatch, status: 'complete' });
    getSearchPreparationTextSamples.mockReturnValue(slowSample.promise);

    render(
      <SearchPreparationReceipt
        projectId="project-1"
        batchId="prep-1"
        onDone={vi.fn()}
        onError={vi.fn()}
      />,
    );

    await act(async () => { await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });
    expect(getSearchPreparationTextSamples).toHaveBeenCalledTimes(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
    expect(getSearchPreparationTextSamples).toHaveBeenCalledTimes(1);

    await act(async () => {
      slowSample.resolve({
        schemaVersion: 'frisket.search_preparation_text_samples.v1',
        batch_id: 'prep-1',
        samples: [{
          row_id: 42,
          column_id: 5,
          filename: 'native.pdf',
          page_number: 1,
          page_count: 1,
          page_scoped: true,
          preview_url: '/api/projects/project-1/blobs/hash/pages/1/image',
          text: 'Slow native text',
          source: 'native',
          truncated: false,
        }],
      });
    });
    expect(screen.getByText('Slow native text')).not.toBeNull();
  });

  it('keeps page sampling controls for whole-PDF preparation items', async () => {
    getSearchPreparation.mockResolvedValue({ ...consentBatch, status: 'complete' });
    const wholePdfSample = {
      row_id: 42,
      column_id: 5,
      filename: 'whole.pdf',
      page_number: 1,
      page_count: 4,
      page_scoped: false,
      preview_url: '/api/projects/project-1/blobs/hash/pages/1/image',
      text: 'First page text',
      source: 'native' as const,
      truncated: false,
    };
    getSearchPreparationTextSamples
      .mockResolvedValueOnce({
        schemaVersion: 'frisket.search_preparation_text_samples.v1',
        batch_id: 'prep-1',
        samples: [wholePdfSample],
      })
      .mockResolvedValueOnce({
        schemaVersion: 'frisket.search_preparation_text_samples.v1',
        batch_id: 'prep-1',
        samples: [{
          ...wholePdfSample,
          page_number: 2,
          preview_url: '/api/projects/project-1/blobs/hash/pages/2/image',
          text: 'Second page text',
        }],
      });

    render(
      <SearchPreparationReceipt
        projectId="project-1"
        batchId="prep-1"
        onDone={vi.fn()}
        onError={vi.fn()}
      />,
    );

    await screen.findByText('First page text');
    fireEvent.click(screen.getByRole('button', { name: 'Page 2' }));
    await screen.findByText('Second page text');
    expect(getSearchPreparationTextSamples).toHaveBeenLastCalledWith(
      'project-1',
      'prep-1',
      { rowId: 42, pageNumber: 2 },
      { signal: expect.anything() },
    );
    fireEvent.click(screen.getByRole('button', { name: 'Yes, keep it' }));
    expect(screen.queryByTestId('search-preparation-text-preview')).toBeNull();
    expect(createSearchPreparation).not.toHaveBeenCalled();
  });

  it('keeps the latest selected PDF page when responses arrive out of order', async () => {
    getSearchPreparation.mockResolvedValue({ ...consentBatch, status: 'complete' });
    const page2 = deferred<Awaited<ReturnType<typeof getSearchPreparationTextSamples>>>();
    const page3 = deferred<Awaited<ReturnType<typeof getSearchPreparationTextSamples>>>();
    const sample = {
      row_id: 42,
      column_id: 5,
      filename: 'whole.pdf',
      page_number: 1,
      page_count: 4,
      page_scoped: false,
      preview_url: '/api/projects/project-1/blobs/hash/pages/1/image',
      text: 'First page text',
      source: 'native' as const,
      truncated: false,
    };
    getSearchPreparationTextSamples
      .mockResolvedValueOnce({
        schemaVersion: 'frisket.search_preparation_text_samples.v1',
        batch_id: 'prep-1',
        samples: [sample],
      })
      .mockReturnValueOnce(page2.promise)
      .mockReturnValueOnce(page3.promise);

    render(
      <SearchPreparationReceipt
        projectId="project-1"
        batchId="prep-1"
        onDone={vi.fn()}
        onError={vi.fn()}
      />,
    );
    await screen.findByText('First page text');
    fireEvent.click(screen.getByRole('button', { name: 'Page 2' }));
    fireEvent.click(screen.getByRole('button', { name: 'Page 3' }));

    await act(async () => {
      page3.resolve({
        schemaVersion: 'frisket.search_preparation_text_samples.v1',
        batch_id: 'prep-1',
        samples: [{ ...sample, page_number: 3, text: 'Third page text' }],
      });
    });
    expect(screen.getByText('Third page text')).not.toBeNull();

    await act(async () => {
      page2.resolve({
        schemaVersion: 'frisket.search_preparation_text_samples.v1',
        batch_id: 'prep-1',
        samples: [{ ...sample, page_number: 2, text: 'Second page text' }],
      });
    });
    expect(screen.getByText('Third page text')).not.toBeNull();
    expect(screen.queryByText('Second page text')).toBeNull();
  });

  it('retries the initial model-pull lookup after a transient failure', async () => {
    vi.useFakeTimers();
    getSearchPreparation.mockResolvedValue({
      ...consentBatch,
      status: 'preparing',
      models: [{ ...consentBatch.models[0], pull_id: 77 }],
    });
    getModelPull
      .mockRejectedValueOnce(new Error('temporary outage'))
      .mockResolvedValueOnce({
        id: 77,
        model: consentBatch.models[0].setup_ref,
        display_name: 'RapidOCR',
        status: 'done',
        phase: null,
        completed_bytes: 10,
        total_bytes: 10,
        resolved_size: 10,
        cancel_requested: false,
        error: null,
        capabilities: { cancel: false, retry: false, remove: false },
      });

    render(
      <SearchPreparationReceipt
        projectId="project-1"
        batchId="prep-1"
        onDone={vi.fn()}
        onError={vi.fn()}
      />,
    );
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole('alert').textContent).toContain('Retrying');

    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(screen.getByTestId('model-pull-done').textContent).toContain('RapidOCR installed');
    expect(getModelPull).toHaveBeenCalledTimes(2);
  });
});
