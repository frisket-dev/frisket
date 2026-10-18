// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  PdfImportCategory,
  PdfImportSnapshot,
  PdfImportsApi,
} from '../../src/api/pdfImports';
import { PdfBatchImportFlow } from '../../src/components/importWorkspace/pdfBatch/PdfBatchImportFlow';
import { canHandlePdfBatch } from '../../src/components/importWorkspace/pdfBatch/model';

vi.mock('../../src/engine-selector/SelectorField', () => ({
  SelectorField: () => <button type="button">RapidOCR</button>,
}));

const categories: Partial<Record<PdfImportCategory, number>> = {
  text: 1,
  suspicious: 1,
  missing: 0,
  image: 0,
  other: 0,
  unreadable: 0,
  unchecked: 0,
};

function snapshot(revision = 1, patch: Partial<PdfImportSnapshot> = {}): PdfImportSnapshot {
  return {
    schema_version: 'frisket.pdf_import.v1',
    id: 'draft-1',
    draft_id: 'draft-1',
    revision,
    updated_at: `2026-10-18T00:00:0${revision}Z`,
    state: 'open',
    paused: false,
    created_at: '2026-10-18T00:00:00Z',
    source_label: '2 PDFs',
    preflight: { state: 'ready', free_bytes: 1_000_000 },
    manifest: { files_done: 2, files_total: 2, bytes_done: 6, bytes_total: 6, sealed: true },
    upload: { files_done: 2, files_total: 2, bytes_done: 6, bytes_total: 6, sealed: true },
    analysis: { files_done: 2, files_total: 2, sealed: true },
    cleanup: { files_done: 0, files_total: 0, bytes_done: 0, bytes_total: 0, sealed: false },
    category_counts: categories,
    sample_ready_counts: { text: 1, suspicious: 1 },
    can_review_text: true,
    choices: {
      ocr_policy: 'missing_or_suspicious',
      ocr_engine: 'rapidocr',
      semantic_search: true,
      semantic_setup_confirmation: null,
      ocr_confirmation: null,
      destination: { kind: 'new', sheet_name: 'Imported PDFs' },
    },
    destination: null,
    match: { state: 'idle', checked: 0, total: 0, matched_count: 0, new_count: 2 },
    semantic_setup: {
      setup_ref: 'embeddinggemma',
      display_name: 'EmbeddingGemma',
      ready: false,
      runtime_ready: true,
      download_bytes: 320_000_000,
      additional_runtime_download: false,
      needs_confirmation: true,
      confirmation_hash: 'semantic-setup-1',
      license: 'Apache-2.0',
      source_url: null,
      available: true,
    },
    batch_ocr: {
      state: 'idle',
      engine: null,
      ocr_policy: null,
      manifest_revision: null,
      manifest_hash: null,
      selected_through: 0,
      selected_count: 0,
      estimate: null,
      needs_confirmation: false,
    },
    preview_jobs: [],
    commit: null,
    ...patch,
  };
}

function createApi(initial = snapshot()) {
  let current = initial;
  const api: PdfImportsApi = {
    create: vi.fn(async () => current),
    addManifest: vi.fn(async () => current),
    sealManifest: vi.fn(async () => current),
    list: vi.fn(async () => [current]),
    get: vi.fn(async () => current),
    samples: vi.fn(async (_projectId, _draftId, category) => ({
      items: [{
        occurrence_id: category === 'suspicious' ? 'suspicious-1' : 'text-1',
        logical_path: `folder/${category}.pdf`,
        filename: `${category}.pdf`,
        size: 3,
        page_count: 2,
        category,
        analysis_state: 'ready',
        sample_pages: [1],
        thumbnail_url: '/thumbnail',
        error: null,
      }],
      next_cursor: null,
    })),
    reconcileReselection: vi.fn(async () => ({
      schema_version: 'frisket.pdf_import.v1', draft_id: 'draft-1', items: [],
    })),
    upload: vi.fn(async () => current),
    pause: vi.fn(async () => {
      current = snapshot(current.revision + 1, { paused: true });
      return current;
    }),
    resume: vi.fn(async () => {
      current = snapshot(current.revision + 1, { paused: false });
      return current;
    }),
    updateChoices: vi.fn(async (_projectId, _draftId, choices) => {
      current = snapshot(current.revision + 1, { choices });
      return current;
    }),
    refreshSemanticSetup: vi.fn(async () => current),
    estimateBatchOcr: vi.fn(async () => {
      current = snapshot(current.revision + 1, {
        batch_ocr: {
          state: 'ready', engine: 'rapidocr', ocr_policy: 'missing_or_suspicious',
          manifest_revision: current.revision, manifest_hash: 'manifest-1', estimate: null,
          selected_through: 2, selected_count: 1, needs_confirmation: false,
        },
      });
      return {
        engine: 'rapidocr', ocr_policy: 'missing_or_suspicious', manifest_revision: current.revision,
        manifest_hash: 'manifest-1', selected_through: 2, selected_count: 1,
        estimate: null, needs_confirmation: false,
      };
    }),
    match: vi.fn(async () => current),
    getPage: vi.fn(async () => ({
      schema_version: 'frisket.pdf_import.v1', draft_id: 'draft-1', occurrence_id: 'suspicious-1', page: 1,
      native_text: 'Broken native text', ocr_text: null, ocr_engine: null, thumbnail_url: '/thumbnail',
    })),
    thumbnailUrl: vi.fn(() => '/thumbnail'),
    estimateOcrPreview: vi.fn(async () => ({ engine: 'rapidocr', cached: false, estimate: null })),
    startOcrPreview: vi.fn(async () => ({
      schema_version: 'frisket.pdf_import.v1', draft_id: 'draft-1', occurrence_id: 'suspicious-1', page: 1,
      job_id: 'preview-1', status: 'running', receipt_id: null, estimate: null, needs_confirmation: false,
    })),
    discard: vi.fn(async () => {
      current = snapshot(current.revision + 1, { state: 'cancelling' });
      return current;
    }),
    commit: vi.fn(async () => ({
      schema_version: 'frisket.pdf_import.v1', draft_id: 'draft-1', commit_id: 'commit-1',
      status: 'accepted', manifest_hash: 'manifest-1', selected_total: 2, available_through: 2,
      uploads_complete: true, job: { sheet_id: 42 },
    })),
  };
  return { api, current: () => current };
}

function files() {
  return [
    new File(['one'], 'one.pdf', { type: 'application/pdf' }),
    new File(['two'], 'two.pdf', { type: 'application/pdf' }),
  ];
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  if (!globalThis.crypto?.randomUUID) {
    let id = 0;
    vi.stubGlobal('crypto', { randomUUID: () => `uuid-${++id}` });
  }
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('PDF batch import flow', () => {
  it('only intercepts selections containing two or more PDFs', () => {
    expect(canHandlePdfBatch(files())).toBe(true);
    expect(canHandlePdfBatch(files().slice(0, 1))).toBe(false);
    expect(canHandlePdfBatch([...files(), new File(['csv'], 'rows.csv', { type: 'text/csv' })])).toBe(false);
  });

  it('declares and seals the bounded manifest before uploading occurrences', async () => {
    const { api } = createApi();
    render(<PdfBatchImportFlow
      projectId="project-1" files={files()} destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    await screen.findByRole('heading', { name: 'Add files' });
    await waitFor(() => expect(api.upload).toHaveBeenCalledTimes(2));
    expect(api.create).toHaveBeenCalledWith(
      'project-1', 2, 6, '2 PDFs', expect.anything(),
    );
    expect(api.addManifest).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.addManifest).mock.calls[0][2]).toHaveLength(2);
    expect(vi.mocked(api.sealManifest).mock.invocationCallOrder[0])
      .toBeLessThan(vi.mocked(api.upload).mock.invocationCallOrder[0]);
  });

  it('preserves choices on Back and commits only after semantic download consent', async () => {
    const { api } = createApi();
    const imported = vi.fn();
    render(<PdfBatchImportFlow
      projectId="project-1" files={files()} destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={imported}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    fireEvent.click(await screen.findByRole('button', { name: 'Check text →' }));
    expect(await screen.findByText('Broken native text')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('radio', { name: /None/ }));
    await waitFor(() => expect(api.updateChoices).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: 'Continue →' }));
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(screen.getByRole('radio', { name: /None/ })).toBeChecked();

    fireEvent.click(screen.getByRole('button', { name: 'Continue →' }));
    fireEvent.click(screen.getByRole('button', { name: 'Search setup →' }));
    const importButton = await screen.findByRole('button', { name: 'Import 2 PDFs' });
    expect(importButton).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: /Download this model/ }));
    await waitFor(() => expect(importButton).toBeEnabled());
    fireEvent.click(importButton);

    await waitFor(() => expect(api.commit).toHaveBeenCalled());
    expect(vi.mocked(api.updateChoices).mock.calls.some((call) => (
      call[2].semantic_search
      && call[2].semantic_setup_confirmation === 'semantic-setup-1'
      && call[2].ocr_policy === 'none'
    ))).toBe(true);
    expect(vi.mocked(api.commit).mock.calls[0][2]).toEqual(expect.objectContaining({
      idempotency_key: expect.any(String), expected_revision: expect.any(Number),
    }));
    await waitFor(() => expect(imported).toHaveBeenCalledWith(42));
  });

  it('pauses for close confirmation and aborts the browser upload before discard', async () => {
    const { api } = createApi();
    let uploadSignal: AbortSignal | undefined;
    vi.mocked(api.upload).mockImplementation(async (...args) => {
      uploadSignal = args[4]?.signal;
      return new Promise<PdfImportSnapshot>((_resolve, reject) => {
        uploadSignal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
      });
    });
    let guard: (() => boolean) | null = null;
    const onError = vi.fn();
    render(<PdfBatchImportFlow
      projectId="project-1" files={files()} destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={(next) => { guard = next; }}
      onImported={vi.fn()} onDiscarded={vi.fn()} onError={onError}
    />);

    await screen.findByRole('heading', { name: 'Add files' });
    await waitFor(() => expect(uploadSignal).toBeDefined());
    expect(guard?.()).toBe(false);
    expect(await screen.findByTestId('pdf-batch-discard-dialog')).toBeInTheDocument();
    await waitFor(() => expect(api.pause).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: 'Discard import' }));

    await waitFor(() => expect(api.discard).toHaveBeenCalled());
    expect(uploadSignal?.aborted).toBe(true);
    expect(onError).not.toHaveBeenCalled();
  });

  it('reconciles a resumed selection and uploads only matched pending occurrences', async () => {
    const initial = snapshot(3, {
      paused: true,
      upload: { files_done: 1, files_total: 2, bytes_done: 3, bytes_total: 6, sealed: false },
      analysis: { files_done: 1, files_total: 2 },
    });
    const { api } = createApi(initial);
    vi.mocked(api.reconcileReselection).mockResolvedValue({
      schema_version: 'frisket.pdf_import.v1',
      draft_id: 'draft-1',
      items: [{
        logical_path: 'two.pdf', filename: 'two.pdf', size: 3,
        matched: true, occurrence_id: 'pending-two', upload_needed: true,
      }],
    });
    render(<PdfBatchImportFlow
      projectId="project-1" resumeDraftId="draft-1" destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    await screen.findByText('Some files still need to be copied');
    const input = document.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input).not.toBeNull();
    fireEvent.change(input!, { target: { files: [new File(['two'], 'two.pdf', { type: 'application/pdf' })] } });

    await waitFor(() => expect(api.reconcileReselection).toHaveBeenCalledWith(
      'project-1', 'draft-1', [{ logical_path: 'two.pdf', filename: 'two.pdf', size: 3 }], expect.anything(),
    ));
    await waitFor(() => expect(api.upload).toHaveBeenCalled());
    expect(vi.mocked(api.upload).mock.calls[0][2]).toEqual([
      expect.objectContaining({ fileId: 'pending-two', logicalPath: 'two.pdf' }),
    ]);
  });
});
