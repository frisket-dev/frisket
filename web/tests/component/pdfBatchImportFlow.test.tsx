// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useEffect, useRef } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  PdfImportCategory,
  PdfImportSnapshot,
  PdfImportsApi,
} from '../../src/api/pdfImports';
import { PdfBatchImportFlow } from '../../src/components/importWorkspace/pdfBatch/PdfBatchImportFlow';
import { bytesLabel, canHandlePdfBatch } from '../../src/components/importWorkspace/pdfBatch/model';

const selectorState = vi.hoisted(() => ({ canRun: true, blocker: null as string | null }));
const sessionUpload = vi.hoisted(() => ({
  abort: null as (() => void) | null,
  importRef: null as string | null,
  remember: vi.fn(async () => undefined),
  setUploading: vi.fn(),
  unregister: vi.fn(),
}));

vi.mock('../../src/components/importProgressSession', () => ({
  registerImportSessionUploadAbort: (importRef: string, abort: () => void) => {
    sessionUpload.importRef = importRef;
    sessionUpload.abort = abort;
    return sessionUpload.unregister;
  },
  rememberImportSessionFiles: sessionUpload.remember,
  setImportSessionBrowserUploading: sessionUpload.setUploading,
}));

vi.mock('../../src/engine-selector/SelectorField', () => ({
  SelectorField: ({ query, onCurrentChoiceChange }: {
    query: { subject: { params: { engine: string; model?: string | null } } };
    onCurrentChoiceChange?(choice: unknown): void;
  }) => {
    const { engine, model } = query.subject.params;
    const hasModel = Object.hasOwn(query.subject.params, 'model');
    const report = useRef(onCurrentChoiceChange);
    report.current = onCurrentChoiceChange;
    useEffect(() => {
      report.current?.({
        authored_selection: hasModel
          ? { kind: 'engine_model', engine, model: model ?? null }
          : { kind: 'engine', engine },
        can_run: selectorState.canRun,
        blocker: selectorState.blocker ? { message: selectorState.blocker } : null,
      });
    }, [engine, hasModel, model]);
    return <button type="button">{engine}</button>;
  },
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
      ocr_selection: { kind: 'engine', engine: 'rapidocr' },
      semantic_search: true,
      semantic_setup_confirmation: null,
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
      current = { ...current, revision: current.revision + 1, paused: true };
      return current;
    }),
    resume: vi.fn(async () => {
      current = { ...current, revision: current.revision + 1, paused: false };
      return current;
    }),
    updateChoices: vi.fn(async (_projectId, _draftId, choices) => {
      current = { ...current, revision: current.revision + 1, choices };
      return current;
    }),
    refreshSemanticSetup: vi.fn(async () => current),
    estimateBatchOcr: vi.fn(async () => {
      current = { ...current, revision: current.revision + 1,
        batch_ocr: {
          state: 'ready', ocr_policy: 'missing_or_suspicious',
          manifest_revision: current.revision, manifest_hash: 'manifest-1', estimate: null,
          selected_through: 2, selected_count: 1, needs_confirmation: false,
        },
      };
      return {
        ocr_policy: 'missing_or_suspicious', manifest_revision: current.revision,
        manifest_hash: 'manifest-1', selected_through: 2, selected_count: 1,
        estimate: null, needs_confirmation: false,
      };
    }),
    match: vi.fn(async () => current),
    getPage: vi.fn(async () => ({
      schema_version: 'frisket.pdf_import.v1', draft_id: 'draft-1', occurrence_id: 'suspicious-1', page: 1,
      native_text: 'Broken native text', ocr_text: null, ocr_action_params: null, thumbnail_url: '/thumbnail',
    })),
    thumbnailUrl: vi.fn(() => '/thumbnail'),
    estimateOcrPreview: vi.fn(async () => ({ ocr_action_params: { engine: 'rapidocr' }, cached: false, estimate: null })),
    startOcrPreview: vi.fn(async () => ({
      schema_version: 'frisket.pdf_import.v1', draft_id: 'draft-1', occurrence_id: 'suspicious-1', page: 1,
      ocr_action_params: { engine: 'rapidocr' }, job_id: 'preview-1', status: 'running', receipt_id: null,
      estimate: null, needs_confirmation: false,
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

function deferred<Value>() {
  let resolve!: (value: Value) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<Value>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  selectorState.canRun = true;
  selectorState.blocker = null;
  sessionUpload.abort = null;
  sessionUpload.importRef = null;
  sessionUpload.remember.mockReset();
  sessionUpload.remember.mockResolvedValue(undefined);
  sessionUpload.setUploading.mockReset();
  sessionUpload.unregister.mockReset();
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
    expect(bytesLabel(0)).toBe('0 B');
  });

  it('declares and seals the bounded manifest before uploading occurrences', async () => {
    const { api } = createApi(snapshot(1, {
      manifest: { files_done: 0, files_total: 2, bytes_done: 0, bytes_total: 6, sealed: false },
    }));
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

  it('does not create another draft when parent callback identities change', async () => {
    const { api } = createApi();
    const selected = files();
    const view = render(<PdfBatchImportFlow
      projectId="project-1" files={selected} destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    await waitFor(() => expect(api.upload).toHaveBeenCalledTimes(2));
    view.rerender(<PdfBatchImportFlow
      projectId="project-1" files={selected} destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    await waitFor(() => expect(api.create).toHaveBeenCalledTimes(1));
  });

  it('ignores a late create response after the project changes', async () => {
    const { api } = createApi();
    const oldCreate = deferred<PdfImportSnapshot>();
    const oldSnapshot = snapshot(1, { id: 'draft-old', draft_id: 'draft-old' });
    const newSnapshot = snapshot(1, {
      id: 'draft-new', draft_id: 'draft-new',
      manifest: { files_done: 0, files_total: 2, bytes_done: 0, bytes_total: 6, sealed: false },
    });
    vi.mocked(api.create).mockImplementation(async (projectId) => (
      projectId === 'project-old' ? oldCreate.promise : newSnapshot
    ));
    vi.mocked(api.addManifest).mockImplementation(async (_projectId, draftId) => (
      draftId === 'draft-new' ? newSnapshot : oldSnapshot
    ));
    vi.mocked(api.sealManifest).mockImplementation(async (_projectId, draftId) => (
      draftId === 'draft-new' ? newSnapshot : oldSnapshot
    ));
    vi.mocked(api.upload).mockImplementation(async (_projectId, draftId) => (
      draftId === 'draft-new' ? newSnapshot : oldSnapshot
    ));
    const selected = files();
    const view = render(<PdfBatchImportFlow
      projectId="project-old" files={selected} destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);
    await waitFor(() => expect(api.create).toHaveBeenCalledWith(
      'project-old', 2, 6, '2 PDFs', expect.anything(),
    ));

    view.rerender(<PdfBatchImportFlow
      projectId="project-new" files={selected} destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);
    await waitFor(() => expect(api.addManifest).toHaveBeenCalledWith(
      'project-new', 'draft-new', expect.anything(), expect.any(String), expect.anything(),
    ));
    oldCreate.resolve(oldSnapshot);

    await waitFor(() => expect(api.upload).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.addManifest).mock.calls.some((call) => call[1] === 'draft-old')).toBe(false);
    expect(vi.mocked(api.upload).mock.calls.some((call) => call[1] === 'draft-old')).toBe(false);
  });

  it('replaces rendered draft state and file inventory when the project changes', async () => {
    const { api } = createApi();
    const oldSnapshot = snapshot(1, { id: 'draft-old', draft_id: 'draft-old' });
    const newSnapshot = snapshot(1, {
      id: 'draft-new',
      draft_id: 'draft-new',
      preflight: { state: 'insufficient', free_bytes: 0 },
    });
    vi.mocked(api.create).mockImplementation(async (projectId) => (
      projectId === 'project-old' ? oldSnapshot : newSnapshot
    ));
    vi.mocked(api.addManifest).mockImplementation(async (_projectId, draftId) => (
      draftId === 'draft-old' ? oldSnapshot : newSnapshot
    ));
    vi.mocked(api.sealManifest).mockImplementation(async (_projectId, draftId) => (
      draftId === 'draft-old' ? oldSnapshot : newSnapshot
    ));
    vi.mocked(api.upload).mockImplementation(async (_projectId, draftId) => (
      draftId === 'draft-old' ? oldSnapshot : newSnapshot
    ));
    const view = render(<PdfBatchImportFlow
      projectId="project-old" files={files()} destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);
    expect(await screen.findByText(/Enough space/)).toBeInTheDocument();

    const replacement = [
      new File(['four'], 'new-one.pdf', { type: 'application/pdf' }),
      new File(['four'], 'new-two.pdf', { type: 'application/pdf' }),
    ];
    view.rerender(<PdfBatchImportFlow
      projectId="project-new" files={replacement} destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    expect(await screen.findByText('Not enough disk space')).toBeInTheDocument();
    expect(api.create).toHaveBeenLastCalledWith(
      'project-new', 2, 8, '2 PDFs', expect.anything(),
    );
    expect(vi.mocked(api.upload).mock.calls.filter((call) => call[1] === 'draft-new').map(
      (call) => call[2][0].file.name,
    )).toEqual(['new-one.pdf', 'new-two.pdf']);
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

  it('keeps late browser uploads alive after commit closes the flow', async () => {
    const initial = snapshot(2, {
      upload: { files_done: 0, files_total: 2, bytes_done: 0, bytes_total: 6, sealed: true },
      analysis: { files_done: 0, files_total: 2 },
    });
    const { api } = createApi(initial);
    const firstUpload = deferred<PdfImportSnapshot>();
    let uploadSignal: AbortSignal | undefined;
    vi.mocked(api.upload).mockImplementationOnce(async (...args) => {
      uploadSignal = args[4]?.signal;
      return firstUpload.promise;
    });
    vi.mocked(api.commit).mockResolvedValue({
      schema_version: 'frisket.pdf_import.v1', draft_id: 'draft-1', commit_id: 'commit-1',
      status: 'accepted', manifest_hash: 'manifest-1', selected_total: 2, available_through: 0,
      uploads_complete: false, job: { sheet_id: 42, import_ref: 'import-session-1' },
    });
    const imported = vi.fn();
    const view = render(<PdfBatchImportFlow
      projectId="project-1" files={files()} destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={imported}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    await waitFor(() => expect(uploadSignal).toBeDefined());
    fireEvent.click(screen.getByRole('button', { name: 'Check text →' }));
    await screen.findByText('Broken native text');
    fireEvent.click(screen.getByRole('radio', { name: /None/ }));
    await waitFor(() => expect(api.updateChoices).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: 'Continue →' }));
    const searchSetup = screen.getByRole('button', { name: 'Search setup →' });
    await waitFor(() => expect(searchSetup).toBeEnabled());
    fireEvent.click(searchSetup);
    await screen.findByRole('heading', { name: 'Search setup' });
    fireEvent.click(screen.getByRole('checkbox', { name: /Search by meaning/ }));
    const importButton = await screen.findByRole('button', { name: 'Import 2 PDFs' });
    await waitFor(() => expect(importButton).toBeEnabled());
    fireEvent.click(importButton);

    await waitFor(() => expect(imported).toHaveBeenCalledWith(42));
    expect(sessionUpload.importRef).toBe('import-session-1');
    expect(sessionUpload.remember).toHaveBeenCalledWith('import-session-1', expect.arrayContaining([
      expect.objectContaining({ name: 'one.pdf' }),
      expect.objectContaining({ name: 'two.pdf' }),
    ]));
    expect(sessionUpload.setUploading).toHaveBeenCalledWith('import-session-1', true);
    view.unmount();
    expect(uploadSignal?.aborted).toBe(false);

    firstUpload.resolve(initial);
    await waitFor(() => expect(api.upload).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(sessionUpload.unregister).toHaveBeenCalledTimes(1));
    expect(sessionUpload.setUploading).toHaveBeenLastCalledWith('import-session-1', false);
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
    expect(screen.getByText('Select folder again')).toBeInTheDocument();
    fireEvent.change(input!, { target: { files: [new File(['two'], 'two.pdf', { type: 'application/pdf' })] } });

    await waitFor(() => expect(api.reconcileReselection).toHaveBeenCalledWith(
      'project-1', 'draft-1', [{ logical_path: 'two.pdf', filename: 'two.pdf', size: 3 }], expect.anything(),
    ));
    await waitFor(() => expect(api.upload).toHaveBeenCalled());
    expect(vi.mocked(api.upload).mock.calls[0][2]).toEqual([
      expect.objectContaining({ fileId: 'pending-two', logicalPath: 'two.pdf' }),
    ]);
  });

  it('replaces progress cards with recoverable disk preflight actions', async () => {
    const initial = snapshot(3, {
      preflight: { state: 'insufficient', free_bytes: 1 },
      manifest: { files_done: 2, files_total: 2, bytes_done: 6, bytes_total: 6, sealed: false },
      upload: { files_done: 0, files_total: 2, bytes_done: 0, bytes_total: 6, sealed: false },
      analysis: { files_done: 0, files_total: 2, sealed: false },
    });
    const { api } = createApi(initial);
    render(<PdfBatchImportFlow
      projectId="project-1" resumeDraftId="draft-1" destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    expect(await screen.findByText('Not enough disk space')).toBeInTheDocument();
    expect(screen.queryByText('Copying files')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
    await waitFor(() => expect(api.sealManifest).toHaveBeenCalledWith('project-1', 'draft-1'));
    fireEvent.click(screen.getByRole('button', { name: 'Choose different files' }));
    expect(await screen.findByTestId('pdf-batch-discard-dialog')).toBeInTheDocument();
  });

  it('resumes a paused fresh copy with the retained browser files', async () => {
    const initial = snapshot(1, {
      upload: { files_done: 0, files_total: 2, bytes_done: 0, bytes_total: 6, sealed: false },
      analysis: { files_done: 0, files_total: 2 },
      category_counts: { unchecked: 2 },
      sample_ready_counts: {},
      can_review_text: false,
    });
    const { api } = createApi(initial);
    vi.mocked(api.upload).mockImplementationOnce(async (...args) => new Promise<PdfImportSnapshot>((_resolve, reject) => {
      args[4]?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }));
    render(<PdfBatchImportFlow
      projectId="project-1" files={files()} destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    fireEvent.click(await screen.findByRole('button', { name: 'Pause' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Resume' }));

    await waitFor(() => expect(api.upload).toHaveBeenCalledTimes(3));
    expect(api.reconcileReselection).not.toHaveBeenCalled();
    expect(vi.mocked(api.upload).mock.calls[1][2]).toHaveLength(1);
    expect(vi.mocked(api.upload).mock.calls[2][2]).toHaveLength(1);
  });

  it('pauses during manifest declaration before replaying one intake owner on Resume', async () => {
    const initial = snapshot(1, {
      manifest: { files_done: 0, files_total: 2, bytes_done: 0, bytes_total: 6, sealed: false },
      upload: { files_done: 0, files_total: 2, bytes_done: 0, bytes_total: 6, sealed: false },
      analysis: { files_done: 0, files_total: 2, sealed: false },
      category_counts: { unchecked: 2 },
      sample_ready_counts: {},
      can_review_text: false,
    });
    const { api } = createApi(initial);
    let manifestSignal: AbortSignal | undefined;
    vi.mocked(api.addManifest)
      .mockImplementationOnce(async (...args) => {
        manifestSignal = args[4]?.signal;
        return new Promise<PdfImportSnapshot>((_resolve, reject) => {
          manifestSignal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
        });
      })
      .mockResolvedValue(initial);
    render(<PdfBatchImportFlow
      projectId="project-1" files={files()} destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    await waitFor(() => expect(manifestSignal).toBeDefined());
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    expect(await screen.findByRole('button', { name: 'Resume' })).toBeInTheDocument();
    expect(manifestSignal?.aborted).toBe(true);
    expect(api.sealManifest).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(api.upload).toHaveBeenCalledTimes(2));
    expect(api.addManifest).toHaveBeenCalledTimes(2);
    expect(api.sealManifest).toHaveBeenCalledTimes(1);
    expect(api.reconcileReselection).not.toHaveBeenCalled();
  });

  it('preserves the saved destination when editing choices in a resumed draft', async () => {
    const initial = snapshot(4, {
      choices: {
        ...snapshot().choices,
        destination: { kind: 'existing', sheet_id: 17 },
      },
      match: { state: 'ready', checked: 2, total: 2, matched_count: 1, new_count: 1 },
    });
    const { api } = createApi(initial);
    render(<PdfBatchImportFlow
      projectId="project-1" resumeDraftId="draft-1" destinationSheets={[{ id: 17, name: 'Archive' }]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    fireEvent.click(await screen.findByRole('button', { name: 'Check text →' }));
    await screen.findByText('Broken native text');
    fireEvent.click(screen.getByRole('radio', { name: /None/ }));

    await waitFor(() => expect(api.updateChoices).toHaveBeenCalledWith(
      'project-1',
      'draft-1',
      expect.objectContaining({ destination: { kind: 'existing', sheet_id: 17 } }),
    ));
  });

  it('does not let an older category response replace the current samples', async () => {
    const { api } = createApi();
    let resolveOld: ((value: Awaited<ReturnType<PdfImportsApi['samples']>>) => void) | undefined;
    vi.mocked(api.samples).mockImplementation(async (_projectId, _draftId, category) => {
      if (category === 'suspicious') return new Promise((resolve) => { resolveOld = resolve; });
      return {
        items: [{
          occurrence_id: 'text-current', logical_path: 'folder/text.pdf', filename: 'text.pdf', size: 3,
          page_count: 1, category: 'text', analysis_state: 'done', sample_pages: [1],
          thumbnail_url: '/text-thumbnail', error: null,
        }],
        next_cursor: null,
      };
    });
    render(<PdfBatchImportFlow
      projectId="project-1" files={files()} destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    fireEvent.click(await screen.findByRole('button', { name: 'Check text →' }));
    fireEvent.click(screen.getByRole('button', { name: /PDFs with a text layer/ }));
    expect((await screen.findAllByText('text.pdf')).length).toBeGreaterThan(0);
    resolveOld?.({
      items: [{
        occurrence_id: 'old', logical_path: 'folder/old.pdf', filename: 'old.pdf', size: 3,
        page_count: 1, category: 'suspicious', analysis_state: 'done', sample_pages: [1],
        thumbnail_url: '/old-thumbnail', error: null,
      }],
      next_cursor: null,
    });

    await waitFor(() => expect(screen.queryByText('old.pdf')).not.toBeInTheDocument());
    expect(screen.getAllByText('text.pdf').length).toBeGreaterThan(0);
  });

  it('keeps engine and model identity through OCR preview estimate, job, and page cache', async () => {
    const selection = { kind: 'engine_model' as const, engine: 'remote-ocr', model: 'vision-ocr-v2' };
    const initial = snapshot(7, { choices: { ...snapshot().choices, ocr_selection: selection } });
    const { api } = createApi(initial);
    let started = false;
    const completed = snapshot(8, {
      choices: initial.choices,
      preview_jobs: [{
        occurrence_id: 'suspicious-1', page: 1, ocr_action_params: { engine: 'remote-ocr', model: 'vision-ocr-v2' },
        job_id: 'preview-model', status: 'done', receipt_id: null,
      }],
    });
    vi.mocked(api.get).mockImplementation(async () => (started ? completed : initial));
    let resolveCompletedPage: ((value: Awaited<ReturnType<PdfImportsApi['getPage']>>) => void) | undefined;
    vi.mocked(api.getPage).mockImplementation(async () => {
      if (!started) return {
        schema_version: 'frisket.pdf_import.v1', draft_id: 'draft-1', occurrence_id: 'suspicious-1', page: 1,
        native_text: 'Broken native text', ocr_text: null, ocr_action_params: null,
        thumbnail_url: '/thumbnail',
      };
      return new Promise((resolve) => { resolveCompletedPage = resolve; });
    });
    vi.mocked(api.estimateOcrPreview).mockResolvedValue({
      ocr_action_params: { engine: 'remote-ocr', model: 'vision-ocr-v2' }, cached: false, estimate: null,
    });
    vi.mocked(api.startOcrPreview).mockImplementation(async () => {
      started = true;
      return {
        schema_version: 'frisket.pdf_import.v1', draft_id: 'draft-1', occurrence_id: 'suspicious-1', page: 1,
        ocr_action_params: { engine: 'remote-ocr', model: 'vision-ocr-v2' }, job_id: 'preview-model',
        status: 'running', receipt_id: null, estimate: null, needs_confirmation: false,
      };
    });
    render(<PdfBatchImportFlow
      projectId="project-1" resumeDraftId="draft-1" destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    fireEvent.click(await screen.findByRole('button', { name: 'Check text →' }));
    await screen.findByText('Broken native text');
    fireEvent.click(screen.getByRole('button', { name: 'OCR preview' }));

    await waitFor(() => expect(api.getPage).toHaveBeenCalledTimes(2));
    resolveCompletedPage?.({
      schema_version: 'frisket.pdf_import.v1', draft_id: 'draft-1', occurrence_id: 'suspicious-1', page: 1,
      native_text: 'Broken native text', ocr_text: 'Model OCR output',
      ocr_action_params: { engine: 'remote-ocr', model: 'vision-ocr-v2' }, thumbnail_url: '/thumbnail',
    });
    expect(await screen.findByText('Model OCR output')).toBeInTheDocument();
    expect(api.estimateOcrPreview).toHaveBeenCalledWith(
      'project-1', 'draft-1', 'suspicious-1', 1, selection,
    );
    expect(api.startOcrPreview).toHaveBeenCalledWith(
      'project-1', 'draft-1', 'suspicious-1', 1, selection, undefined,
    );
  });

  it('persists the complete new sheet name before leaving destination review', async () => {
    const { api } = createApi();
    render(<PdfBatchImportFlow
      projectId="project-1" files={files()} destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    fireEvent.click(await screen.findByRole('button', { name: 'Check text →' }));
    await screen.findByText('Broken native text');
    fireEvent.click(screen.getByRole('button', { name: 'Continue →' }));
    const name = screen.getByLabelText('Sheet name');
    fireEvent.change(name, { target: { value: 'Quarterly' } });
    fireEvent.change(name, { target: { value: 'Quarterly PDFs' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search setup →' }));

    await waitFor(() => expect(api.updateChoices).toHaveBeenCalledWith(
      'project-1',
      'draft-1',
      expect.objectContaining({ destination: { kind: 'new', sheet_name: 'Quarterly PDFs' } }),
    ));
    expect(await screen.findByRole('heading', { name: 'Search setup' })).toBeInTheDocument();
  });

  it('uses the selected folder name as the default new sheet name', async () => {
    const selected = files();
    Object.defineProperty(selected[0], 'webkitRelativePath', { value: 'Client archive/one.pdf' });
    Object.defineProperty(selected[1], 'webkitRelativePath', { value: 'Client archive/two.pdf' });
    const { api } = createApi();
    render(<PdfBatchImportFlow
      projectId="project-1" files={selected} destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    fireEvent.click(await screen.findByRole('button', { name: 'Check text →' }));
    await screen.findByText('Broken native text');
    fireEvent.click(screen.getByRole('button', { name: 'Continue →' }));
    expect(screen.getByLabelText('Sheet name')).toHaveValue('Client archive');
    expect(api.create).toHaveBeenCalledWith(
      'project-1', 2, 6, 'Client archive', expect.anything(),
    );
  });

  it('waits for copied files before paid OCR estimation and offers retry after failure', async () => {
    const initial = snapshot(5, {
      upload: { files_done: 1, files_total: 2, bytes_done: 3, bytes_total: 6, sealed: false },
      analysis: { files_done: 1, files_total: 2, sealed: false },
      choices: { ...snapshot().choices, ocr_selection: { kind: 'engine_model', engine: 'remote-ocr', model: 'vision-ocr-v2' } },
    });
    const { api } = createApi(initial);
    let server = initial;
    vi.mocked(api.get).mockImplementation(async () => server);
    vi.mocked(api.updateChoices).mockImplementation(async (_projectId, _draftId, choices) => {
      const filesReady = !choices.semantic_search;
      server = {
        ...server,
        revision: server.revision + 1,
        choices,
        upload: filesReady
          ? { files_done: 2, files_total: 2, bytes_done: 6, bytes_total: 6, sealed: true }
          : server.upload,
        analysis: filesReady
          ? { files_done: 2, files_total: 2, sealed: true }
          : server.analysis,
      };
      return server;
    });
    vi.mocked(api.refreshSemanticSetup).mockImplementation(async () => server);
    vi.mocked(api.estimateBatchOcr)
      .mockRejectedValueOnce(new Error('OCR estimate unavailable'))
      .mockImplementationOnce(async () => {
        server = {
          ...server,
          revision: server.revision + 1,
          batch_ocr: {
            state: 'ready', ocr_policy: 'missing_or_suspicious',
            manifest_revision: server.revision, manifest_hash: 'manifest-remote', estimate: null,
            selected_through: 2, selected_count: 1, needs_confirmation: false,
          },
        };
        return server.batch_ocr!;
      });
    render(<PdfBatchImportFlow
      projectId="project-1" resumeDraftId="draft-1" destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    fireEvent.click(await screen.findByRole('button', { name: 'Check text →' }));
    await screen.findByText('Broken native text');
    fireEvent.click(screen.getByRole('button', { name: 'Continue →' }));
    fireEvent.click(screen.getByRole('button', { name: 'Search setup →' }));

    expect(await screen.findByText('Waiting for files…')).toBeInTheDocument();
    expect(api.estimateBatchOcr).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('checkbox', { name: /Search by meaning/ }));
    expect(await screen.findByRole('button', { name: 'Try again' })).toHaveAttribute(
      'title', 'OCR estimate unavailable',
    );
    expect(api.estimateBatchOcr).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(api.estimateBatchOcr).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('Estimate ready')).toBeInTheDocument();
  });

  it('blocks import when the selected OCR engine still needs its standard setup', async () => {
    selectorState.canRun = false;
    selectorState.blocker = 'Download RapidOCR before running it.';
    const { api } = createApi();
    render(<PdfBatchImportFlow
      projectId="project-1" resumeDraftId="draft-1" destinationSheets={[]} api={api}
      onContextChange={vi.fn()} registerCloseGuard={vi.fn()} onImported={vi.fn()}
      onDiscarded={vi.fn()} onError={vi.fn()}
    />);

    fireEvent.click(await screen.findByRole('button', { name: 'Check text →' }));
    await screen.findByText('Broken native text');
    fireEvent.click(screen.getByRole('button', { name: 'Continue →' }));
    fireEvent.click(screen.getByRole('button', { name: 'Search setup →' }));

    expect(await screen.findByText('Setup required')).toHaveAttribute(
      'title', 'Download RapidOCR before running it.',
    );
    expect(screen.getByRole('button', { name: 'Import 2 PDFs' })).toBeDisabled();
  });
});
