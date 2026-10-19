// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const prepareImportFiles = vi.hoisted(() => vi.fn());
const executeBulkImport = vi.hoisted(() => vi.fn());
const importFollowTheMoney = vi.hoisted(() => vi.fn());
const importCsv = vi.hoisted(() => vi.fn());
const previewCsv = vi.hoisted(() => vi.fn());
const importFiles = vi.hoisted(() => vi.fn());
const getImportSession = vi.hoisted(() => vi.fn());
const createImportSession = vi.hoisted(() => vi.fn());
const uploadFilesToImportSession = vi.hoisted(() => vi.fn());
const getSearchPreparation = vi.hoisted(() => vi.fn());
const getSearchPreparationTextSamples = vi.hoisted(() => vi.fn());
const createSearchPreparation = vi.hoisted(() => vi.fn());
const replanSearchPreparation = vi.hoisted(() => vi.fn());
const listPdfImportDrafts = vi.hoisted(() => vi.fn());
const createPdfImport = vi.hoisted(() => vi.fn());
const getPdfImport = vi.hoisted(() => vi.fn());
const discardPdfImport = vi.hoisted(() => vi.fn());
const createTableImportFromFile = vi.hoisted(() => vi.fn());
const projectApi = vi.hoisted(() => ({ getSheetData: vi.fn(), listSheets: vi.fn() }));

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    prepareImportFiles,
    executeBulkImport,
    importFollowTheMoney,
    importCsv,
    previewCsv,
    importFiles,
    getImportSession,
    createImportSession,
    uploadFilesToImportSession,
  };
});

vi.mock('../../src/bind/useWorkspaceStores', async () => {
  const { createRouteStore } = await import('../../src/state/routeStore');
  const route = createRouteStore('open-session-project');
  return {
    useWorkspaceStores: () => ({
      projectApi,
      route,
      chromePreferences: { projectId: 'open-session-project' },
    }),
  };
});

vi.mock('../../src/api/searchPreparation', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/searchPreparation')>(),
  getSearchPreparation,
  getSearchPreparationTextSamples,
  createSearchPreparation,
  replanSearchPreparation,
}));

vi.mock('../../src/api/pdfImports', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/pdfImports')>();
  return {
    ...actual,
    pdfImportsApi: {
      ...actual.pdfImportsApi,
      list: listPdfImportDrafts,
      create: createPdfImport,
      get: getPdfImport,
      discard: discardPdfImport,
    },
  };
});

vi.mock('../../src/api/tableImports', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/tableImports')>();
  return {
    ...actual,
    tableImportsApi: {
      ...actual.tableImportsApi,
      createFromFile: createTableImportFromFile,
    },
  };
});

import { ImportDropzone, ImportWorkspaceDialog } from '../../src/components/ImportCsv';

const plan = {
  plan_id: 'open-session-plan',
  questions: [{
    id: 'matching-csv',
    kind: 'csv_combine',
    default: 'combine',
    logical_paths: ['inbox.csv', 'sent.csv'],
  }],
  proposed_outputs: [{
    id: 'messages',
    kind: 'csv_group',
    sheet_name: 'Messages',
    logical_paths: ['inbox.csv', 'sent.csv'],
  }],
};

function renderDialog(open: boolean, onImported = vi.fn(), onClose = vi.fn()) {
  return render(
    <ImportWorkspaceDialog
      open={open}
      entryMode="files"
      onClose={onClose}
      onImported={onImported}
      onError={vi.fn()}
      onLaunchDownload={vi.fn()}
    />,
  );
}

beforeEach(() => {
  listPdfImportDrafts.mockReset();
  listPdfImportDrafts.mockResolvedValue([]);
  createPdfImport.mockReset();
  createPdfImport.mockImplementation(() => new Promise(() => undefined));
  getPdfImport.mockReset();
  getPdfImport.mockImplementation(() => new Promise(() => undefined));
  discardPdfImport.mockReset();
  discardPdfImport.mockResolvedValue({ draft_id: 'zip-pdf-draft', state: 'cancelled' });
  createTableImportFromFile.mockReset();
  createTableImportFromFile.mockImplementation(() => new Promise(() => undefined));
  getSearchPreparationTextSamples.mockReset();
  getSearchPreparationTextSamples.mockResolvedValue({ batch_id: 'prep', samples: [] });
  createSearchPreparation.mockReset();
  replanSearchPreparation.mockReset();
  getImportSession.mockReset();
  getImportSession.mockResolvedValue({
    import_ref: 'large-selection', state: 'running', admitted_files: 4_096, admitted_bytes: 4_096,
    through: 4_096, committed_rows: 0, committed_bytes: 0, sheet_id: null,
    sheet_name: 'Files', cancel_requested: false, sealed: true, error: null,
    search_preparation_batch_id: null,
  });
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value() { this.open = true; },
  });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', {
    configurable: true,
    value() { this.open = false; },
  });
  prepareImportFiles.mockReset();
  prepareImportFiles.mockResolvedValue({ kind: 'bulk', plan });
  importFollowTheMoney.mockResolvedValue({
    schema_version: 'frisket.action_result.v1',
    status: 'completed',
    outputs: [{ kind: 'sheet', sheet_id: 88 }],
    value: { dataset_id: 'dataset-1' },
  });
  previewCsv.mockResolvedValue({
    row_count: 1,
    columns: [{ name: 'name', type: 'text' }],
    preview_rows: [{ name: 'Ada' }],
    encoding: 'utf-8',
    delimiter: ',',
  });
  importCsv.mockRejectedValue(new Error('retryable test failure'));
  createImportSession.mockResolvedValue({
    import_ref: 'large-selection', state: 'admitting', admitted_files: 0, admitted_bytes: 0,
    through: 0, committed_rows: 0, committed_bytes: 0, sheet_id: null,
    sheet_name: 'Files', cancel_requested: false, sealed: false, error: null,
  });
  uploadFilesToImportSession.mockResolvedValue({
    import_ref: 'large-selection', state: 'running', admitted_files: 4_096, admitted_bytes: 4_096,
    through: 4_096, committed_rows: 0, committed_bytes: 0, sheet_id: null,
    sheet_name: 'Files', cancel_requested: false, sealed: true, error: null,
  });
  getSearchPreparation.mockImplementation(async (_projectId: string, batchId: string) => ({
    schemaVersion: 'frisket.search_preparation.v1',
    id: batchId,
    sheet_id: 91,
    plan_hash: 'sha256:plan',
    status: 'complete',
    groups: [{ kind: 'native_pdf', count: 1, status: 'complete' }],
    models: [], jobs: [], can_stop: false, can_setup: true, errors: [],
  }));
  projectApi.getSheetData.mockResolvedValue({ columns: [], total: 0 });
  projectApi.listSheets.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('ImportWorkspaceDialog controlled open session', () => {
  it('shows all import kinds while choosing table data, then gives row review the full workspace', async () => {
    render(
      <ImportWorkspaceDialog
        open
        onClose={vi.fn()}
        onImported={vi.fn()}
        onError={vi.fn()}
        onLaunchDownload={vi.fn()}
      />,
    );

    const workspace = screen.getByTestId('import-workspace');
    expect(screen.getByRole('tablist', { name: 'Import type' })).toBeVisible();
    expect(screen.getByTestId('import-mode-files')).toBeVisible();
    expect(workspace).toHaveClass('table-import-workspace-mode-selection');

    createTableImportFromFile.mockResolvedValueOnce({
      schema_version: 'frisket.table_import.v1',
      id: 'table-draft-1',
      draft_id: 'table-draft-1',
      state: 'ready',
      revision: 1,
      created_at: '2026-10-19T00:00:00Z',
      updated_at: '2026-10-19T00:00:00Z',
      source: {
        input_kind: 'file', filename: 'people.csv', size: 13, sha256: 'sha256:test',
        format: 'delimited', sheets: [], can_rerun_workbook: false,
      },
      settings: {
        revision: 0,
        requested: {
          delimiter: null, encoding: null, decimal_separator: 'auto',
          thousands_separator: 'auto', worksheet: null, skip_rows_start: 0,
          first_row_is_header: true, skip_rows_end: 0,
        },
        resolved: {
          kind: 'delimited', delimiter: ',', encoding: 'utf-8', decimal_separator: '.',
          thousands_separator: ',', worksheet: null, skip_rows_start: 0,
          first_row_is_header: true, skip_rows_end: 0,
        },
      },
      analysis: {
        state: 'complete', job_id: 1, scanned_rows: 1, estimated_rows: 1,
        exact_rows: 1, complete: true,
        issue_counts: { extra_values: 0, missing_values: 0, malformed_records: 0, unreadable_cells: 0 },
        error: null,
      },
      preview: null,
      columns: [],
      destination: null,
      can_commit: false,
      commit_blocked_reason: 'destination',
      commit: null,
    });
    const file = new File(['name\nAda\n'], 'people.csv', { type: 'text/csv' });
    const input = screen.getByRole('button', { name: 'Choose file' })
      .closest('.table-import-dropzone')?.querySelector('input');
    fireEvent.change(input!, { target: { files: [file] } });

    expect(await screen.findByLabelText('Check rows')).toBeVisible();
    await waitFor(() => expect(screen.queryByRole('tablist', { name: 'Import type' })).toBeNull());
    expect(workspace).not.toHaveClass('table-import-workspace-mode-selection');
  });

  it('reopens an existing preparation batch in the import workspace', async () => {
    render(
      <ImportWorkspaceDialog
        open
        initialPreparationBatchId="prep-reopen"
        onClose={vi.fn()}
        onImported={vi.fn()}
        onError={vi.fn()}
        onLaunchDownload={vi.fn()}
      />,
    );

    await waitFor(() => expect(getSearchPreparation).toHaveBeenCalledWith(
      'open-session-project',
      'prep-reopen',
    ));
    expect(screen.getByTestId('search-preparation-receipt')).toBeInTheDocument();
    expect(screen.getByText('Text ready for keyword search')).toBeInTheDocument();
  });

  it('switches between Files and URLs at the top and imports dropped files through the picker path', async () => {
    importFiles.mockResolvedValue({ sheet_id: 91, rows: 1 });
    renderDialog(true);

    fireEvent.click(screen.getByTestId('import-mode-files'));

    expect(screen.getByTestId('import-source-toggle')).toBeVisible();
    expect(screen.getByTestId('import-source-files')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Pick files' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Choose folder' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Upload ZIP' })).toBeVisible();
    expect(screen.queryByText('PDFs are added as documents. Import does not extract PDF tables.')).toBeNull();
    expect(screen.queryByText('Import a list of URLs instead')).toBeNull();

    fireEvent.click(screen.getByTestId('import-source-urls'));
    expect(screen.getByTestId('import-url-list')).toBeVisible();
    expect(screen.getByTestId('import-source-urls')).toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(screen.getByTestId('import-source-files'));
    const dropped = new File(['report'], 'report.txt', { type: 'text/plain' });
    fireEvent.drop(screen.getByTestId('import-files-chooser'), {
      dataTransfer: { files: [dropped] },
    });

    await waitFor(() => expect(importFiles).toHaveBeenCalledWith(
      'open-session-project',
      [dropped],
    ));
  });

  it('routes two PDFs with companion files into one staged batch without dropping companions', async () => {
    renderDialog(true);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    const selected = [
      new File(['one'], 'one.pdf', { type: 'application/pdf' }),
      new File(['two'], 'two.pdf', { type: 'application/pdf' }),
      new File(['img'], 'scan.png', { type: 'image/png' }),
      new File(['txt'], 'notes.txt', { type: 'text/plain' }),
    ];

    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files: selected } });

    await waitFor(() => expect(createPdfImport).toHaveBeenCalledWith(
      'open-session-project', 4, 12, '4 files', expect.anything(),
    ));
    expect(await screen.findByText('Preparing file import…')).toBeInTheDocument();
    expect(createImportSession).not.toHaveBeenCalled();
    expect(prepareImportFiles).not.toHaveBeenCalled();
  });

  it('prepares ZIP selections before routing even when direct siblings include two PDFs', async () => {
    prepareImportFiles.mockResolvedValue({
      kind: 'pdf_import',
      draft: { draft_id: 'zip-pdf-draft' },
    });
    renderDialog(true);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    const selected = [
      new File(['one'], 'one.pdf', { type: 'application/pdf' }),
      new File(['two'], 'two.pdf', { type: 'application/pdf' }),
      new File(['zip-a'], 'first.zip', { type: 'application/zip' }),
      new File(['zip-b'], 'second.zip', { type: 'application/zip' }),
      new File(['notes'], 'notes.txt', { type: 'text/plain' }),
    ];

    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files: selected } });

    await waitFor(() => expect(prepareImportFiles).toHaveBeenCalledWith(
      'open-session-project',
      selected,
      selected.map((file) => file.name),
      true,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
    await waitFor(() => expect(getPdfImport).toHaveBeenCalledWith(
      'open-session-project', 'zip-pdf-draft', expect.anything(),
    ));
    expect(createPdfImport).not.toHaveBeenCalled();
    expect(screen.getByText('Preparing file import…')).toBeInTheDocument();
  });

  it('confirms before cancelling archive preparation and discards a draft that finishes behind the prompt', async () => {
    let resolvePreparation!: (value: { kind: 'pdf_import'; draft: { draft_id: string } }) => void;
    const pendingPreparation = new Promise<{ kind: 'pdf_import'; draft: { draft_id: string } }>((resolve) => {
      resolvePreparation = resolve;
    });
    let preparationSignal: AbortSignal | undefined;
    prepareImportFiles.mockImplementation((...args: unknown[]) => {
      preparationSignal = (args[4] as { signal?: AbortSignal }).signal;
      return pendingPreparation;
    });
    const onClose = vi.fn();
    renderDialog(true, vi.fn(), onClose);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    fireEvent.change(screen.getByTestId('import-file-input'), {
      target: { files: [new File(['zip'], 'records.zip', { type: 'application/zip' })] },
    });

    await waitFor(() => expect(prepareImportFiles).toHaveBeenCalledOnce());
    const close = screen.getByTestId('import-workspace-close');
    expect(close).toBeEnabled();
    fireEvent.click(close);
    expect(screen.getByTestId('bulk-prepare-discard-dialog')).toBeVisible();
    expect(preparationSignal?.aborted).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Keep working' }));
    expect(screen.queryByTestId('bulk-prepare-discard-dialog')).not.toBeInTheDocument();
    expect(preparationSignal?.aborted).toBe(false);

    fireEvent.click(close);
    await act(async () => resolvePreparation({
      kind: 'pdf_import', draft: { draft_id: 'zip-pdf-draft' },
    }));
    expect(getPdfImport).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel import' }));

    await waitFor(() => expect(discardPdfImport).toHaveBeenCalledWith(
      'open-session-project', 'zip-pdf-draft',
    ));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  });

  it('sends every archive selected in the ZIP picker through one preparation request', async () => {
    renderDialog(true);
    const selected = [
      new File(['zip-a'], 'first.zip', { type: 'application/zip' }),
      new File(['zip-b'], 'second.zip', { type: 'application/zip' }),
    ];

    fireEvent.change(screen.getByTestId('import-zip-input'), { target: { files: selected } });

    await waitFor(() => expect(prepareImportFiles).toHaveBeenCalledWith(
      'open-session-project', selected, ['first.zip', 'second.zip'], true,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
  });

  it('keeps mixed selections with fewer than two PDFs on the existing file-import path', async () => {
    renderDialog(true);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    const selected = [
      new File(['one'], 'one.pdf', { type: 'application/pdf' }),
      new File(['img'], 'scan.png', { type: 'image/png' }),
      new File(['txt'], 'notes.txt', { type: 'text/plain' }),
    ];

    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files: selected } });

    await waitFor(() => expect(createImportSession).toHaveBeenCalledWith(
      'open-session-project', { sheetName: 'files' },
    ));
    await waitFor(() => expect(uploadFilesToImportSession).toHaveBeenCalledWith(
      'open-session-project', expect.objectContaining({ import_ref: 'large-selection' }), selected,
      expect.objectContaining({ onStatus: expect.any(Function) }),
    ));
    expect(createPdfImport).not.toHaveBeenCalled();
  });

  it('keeps filenames and finishing progress in the upload window until Next opens review', async () => {
    let finishUpload!: (value: unknown) => void;
    importFiles.mockReturnValue(new Promise((resolve) => { finishUpload = resolve; }));
    const onClose = vi.fn();
    renderDialog(true, vi.fn(), onClose);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    fireEvent.change(screen.getByTestId('import-file-input'), {
      target: { files: [new File(['pdf'], 'report.pdf', { type: 'application/pdf' })] },
    });
    const summary = await screen.findByTestId('import-files-summary');
    expect(summary).toHaveTextContent('report.pdf');
    expect(summary).toHaveTextContent('1 file');
    expect(summary).toHaveTextContent('Importing files…');
    expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument();
    await act(async () => finishUpload({ sheet_id: 91, rows: 1, search_preparation_batch_id: 'pdf-prep' }));
    expect(await screen.findByRole('button', { name: 'Next' })).toBeEnabled();
    expect(screen.queryByText('Importing files…')).not.toBeInTheDocument();
    expect(getSearchPreparation).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(getSearchPreparation).toHaveBeenCalledWith('open-session-project', 'pdf-prep'));
  });

  it('opens the shared preparation receipt after a synchronous file import', async () => {
    importFiles.mockResolvedValue({
      sheet_id: 91,
      rows: 1,
      search_preparation_batch_id: 'sync-prep',
    });
    const onImported = vi.fn();
    renderDialog(true, onImported);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    fireEvent.change(screen.getByTestId('import-file-input'), {
      target: { files: [new File(['report'], 'report.txt', { type: 'text/plain' })] },
    });

    await waitFor(() => expect(onImported).toHaveBeenCalledWith(91));
    fireEvent.click(await screen.findByTestId('import-files-next'));
    const receipt = await screen.findByTestId('search-preparation-receipt');
    expect(receipt).toHaveTextContent('Text ready for keyword search');
    expect(receipt.closest('.import-workspace-main')).toHaveClass('import-workspace-main-preparation');
    expect(screen.queryByText('Project data intake')).not.toBeInTheDocument();
    expect(screen.getByTestId('import-workspace')).toHaveAccessibleName('Import');
    expect(getSearchPreparation).toHaveBeenCalledWith('open-session-project', 'sync-prep');
  });

  it('keeps an automatic ZIP import in the shared receipt and reviews each created sheet', async () => {
    prepareImportFiles.mockResolvedValue({ kind: 'bulk', plan: { ...plan, questions: [] } });
    executeBulkImport.mockResolvedValue({
      created: [
        { id: 'a', kind: 'sheet', sheet_id: 41, sheet_name: 'PDFs', logical_paths: ['report.pdf'], search_preparation_batch_id: 'zip-pdf-prep' },
        { id: 'b', kind: 'sheet', sheet_id: 42, sheet_name: 'Audio', logical_paths: ['interview.mp3'], search_preparation_batch_id: 'zip-audio-prep' },
        { id: 'c', kind: 'sheet', sheet_id: 43, sheet_name: 'Records', logical_paths: ['records.csv'], search_preparation_batch_id: null },
      ],
      failed: [{ logical_paths: ['broken.csv'], logical_paths_omitted: 0, error: 'Invalid header' }],
      failed_omitted: 0, first_sheet_id: 41,
      message: 'Imported 3 files; 1 file failed.', warnings: ['One attachment was skipped.'],
    });
    const onImported = vi.fn();
    const onClose = vi.fn();
    renderDialog(true, onImported, onClose);
    fireEvent.change(screen.getByTestId('import-zip-input'), {
      target: { files: [new File(['archive'], 'documents.zip', { type: 'application/zip' })] },
    });

    await screen.findByTestId('import-files-next');
    expect(getSearchPreparation).not.toHaveBeenCalled();
    expect(onImported).toHaveBeenCalledWith(41);
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByTestId('import-files-summary')).toHaveTextContent('documents.zip');
    expect(screen.getByRole('alert')).toHaveTextContent('Imported 3 files; 1 file failed.');
    expect(screen.getByText('Invalid header')).toBeVisible();
    expect(screen.getAllByText('One attachment was skipped.')).toHaveLength(1);
    fireEvent.click(screen.getByTestId('import-files-next'));
    await screen.findByText('Text ready for keyword search');

    fireEvent.change(screen.getByRole('combobox', { name: 'Review imported sheet' }), { target: { value: '42' } });
    await waitFor(() => expect(getSearchPreparation).toHaveBeenCalledWith('open-session-project', 'zip-audio-prep'));
    fireEvent.change(screen.getByRole('combobox', { name: 'Review imported sheet' }), { target: { value: '43' } });
    expect(screen.getByText('Files imported. Search preparation is not available for this sheet.')).toBeVisible();
    expect(getSearchPreparation).toHaveBeenCalledTimes(2);
    fireEvent.change(screen.getByRole('combobox', { name: 'Review imported sheet' }), { target: { value: '41' } });
    await screen.findByText('Text ready for keyword search');
    fireEvent.click(screen.getByTestId('search-preparation-done'));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('keeps sheet B selected when sheet A replacement OCR finishes after switching sheets', async () => {
    prepareImportFiles.mockResolvedValue({ kind: 'bulk', plan: { ...plan, questions: [] } });
    executeBulkImport.mockResolvedValue({
      created: [
        { id: 'a', kind: 'sheet', sheet_id: 41, sheet_name: 'A', logical_paths: ['a.pdf'], search_preparation_batch_id: 'prep-a' },
        { id: 'b', kind: 'sheet', sheet_id: 42, sheet_name: 'B', logical_paths: ['b.pdf'], search_preparation_batch_id: 'prep-b' },
      ],
      failed: [], failed_omitted: 0, first_sheet_id: 41, message: 'Imported 2 files.', warnings: [],
    });
    const batch = (id: string, sheet_id: number) => ({
      schemaVersion: 'frisket.search_preparation.v1', id, sheet_id, plan_hash: 'sha256:plan',
      status: 'needs_consent', force_ocr: false, groups: [{ kind: 'native_pdf', count: 1, status: 'complete' }],
      models: [], jobs: [], can_stop: false, can_setup: true, errors: [],
    });
    getSearchPreparation.mockImplementation(async (_projectId: string, id: string) => batch(id, id === 'prep-b' ? 42 : 41));
    getSearchPreparationTextSamples.mockImplementation(async (_projectId: string, id: string) => ({
      batch_id: id, samples: [{
        row_id: id === 'prep-a' ? 101 : 102, column_id: 5, filename: `${id}.pdf`, page_number: 1,
        page_count: 1, page_scoped: true, preview_url: '/preview',
        text: `Native text ${id}`, source: 'native', truncated: false,
      }],
    }));
    let resolveOcr!: (value: ReturnType<typeof batch>) => void;
    replanSearchPreparation.mockReturnValue(new Promise((resolve) => { resolveOcr = resolve; }));
    renderDialog(true);
    fireEvent.change(screen.getByTestId('import-zip-input'), {
      target: { files: [new File(['archive'], 'documents.zip', { type: 'application/zip' })] },
    });
    fireEvent.click(await screen.findByTestId('import-files-next'));
    await screen.findByText('Native text prep-a');
    fireEvent.change(screen.getByRole('combobox', { name: 'PDF text' }), {
      target: { value: 'ocr' },
    });
    expect(replanSearchPreparation).toHaveBeenCalledWith('open-session-project', 'prep-a', {
      plan_hash: 'sha256:plan', force_ocr: true,
    });
    fireEvent.change(screen.getByRole('combobox', { name: 'Review imported sheet' }), { target: { value: '42' } });
    await screen.findByText('Native text prep-b');
    await act(async () => { resolveOcr(batch('prep-a-ocr', 41)); });
    expect(screen.getByRole('combobox', { name: 'Review imported sheet' })).toHaveValue('42');
    expect(screen.getByText('Native text prep-b')).toBeVisible();
    expect(getSearchPreparation).not.toHaveBeenCalledWith('open-session-project', 'prep-a-ocr');
  });

  it('opens the receipt for a successful automatic ZIP without warnings', async () => {
    prepareImportFiles.mockResolvedValue({ kind: 'bulk', plan: { ...plan, questions: [] } });
    executeBulkImport.mockResolvedValue({
      created: [{ id: 'pdf', kind: 'sheet', sheet_id: 41, sheet_name: 'PDFs', logical_paths: ['report.pdf'], search_preparation_batch_id: 'zip-success' }],
      failed: [], failed_omitted: 0, first_sheet_id: 41, message: 'Imported 1 file.', warnings: [],
    });
    const onClose = vi.fn();
    renderDialog(true, vi.fn(), onClose);
    fireEvent.change(screen.getByTestId('import-zip-input'), {
      target: { files: [new File(['archive'], 'documents.zip', { type: 'application/zip' })] },
    });
    fireEvent.click(await screen.findByTestId('import-files-next'));
    await screen.findByText('Text ready for keyword search');
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.queryByRole('combobox', { name: 'Review imported sheet' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('search-preparation-done'));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('finishes automatic bulk spreadsheet imports when no preparation batch was created', async () => {
    prepareImportFiles.mockResolvedValue({ kind: 'bulk', plan: { ...plan, questions: [] } });
    executeBulkImport.mockResolvedValue({
      created: [
        { id: 'csv', kind: 'sheet', sheet_id: 41, sheet_name: 'CSV records', logical_paths: ['records.csv'], search_preparation_batch_id: null },
        { id: 'xlsx', kind: 'sheet', sheet_id: 42, sheet_name: 'Workbook', logical_paths: ['records.xlsx'], search_preparation_batch_id: null },
      ],
      failed: [], failed_omitted: 0, first_sheet_id: 41, message: 'Imported 2 files.', warnings: [],
    });
    const onImported = vi.fn();
    const onClose = vi.fn();
    renderDialog(true, onImported, onClose);
    fireEvent.change(screen.getByTestId('import-zip-input'), {
      target: { files: [new File(['archive'], 'records.zip', { type: 'application/zip' })] },
    });
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(onImported).toHaveBeenCalledWith(41);
    expect(getSearchPreparation).not.toHaveBeenCalled();
  });

  it('opens the same receipt after a resumable attachment session seals', async () => {
    uploadFilesToImportSession.mockResolvedValue({
      import_ref: 'large-selection', state: 'completed', admitted_files: 2, admitted_bytes: 2,
      through: 2, committed_rows: 2, committed_bytes: 2, sheet_id: 91,
      sheet_name: 'Files', cancel_requested: false, sealed: true, error: null,
      search_preparation_batch_id: 'resumable-prep',
    });
    const onImported = vi.fn();
    renderDialog(true, onImported);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    fireEvent.change(screen.getByTestId('import-file-input'), {
      target: {
        files: [
          new File(['a'], 'first.txt', { type: 'text/plain' }),
          new File(['b'], 'second.txt', { type: 'text/plain' }),
        ],
      },
    });

    await waitFor(() => expect(onImported).toHaveBeenCalledWith(91));
    fireEvent.click(await screen.findByTestId('import-files-next'));
    expect(await screen.findByTestId('search-preparation-receipt')).toHaveTextContent('Text ready for keyword search');
    expect(getSearchPreparation).toHaveBeenCalledWith('open-session-project', 'resumable-prep');
  });

  it('shows acknowledged upload and saved-row progress without estimating an in-flight chunk', async () => {
    let finishUpload!: (status: unknown) => void;
    getImportSession.mockReturnValue(new Promise(() => {}));
    uploadFilesToImportSession.mockImplementation((
      _projectId: string,
      _session: unknown,
      _files: File[],
      options: { onStatus(status: unknown): void },
    ) => {
      options.onStatus({
        import_ref: 'large-selection', state: 'running', admitted_files: 1, admitted_bytes: 1,
        through: 1, committed_rows: 0, committed_bytes: 0, sheet_id: null,
        sheet_name: 'Files', cancel_requested: false, sealed: false, error: null,
        search_preparation_batch_id: null, search_preparation_status: 'pending',
      });
      return new Promise((resolve) => { finishUpload = resolve; });
    });
    renderDialog(true);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files: [
      new File(['a'], 'a.txt', { type: 'text/plain' }),
      new File(['b'], 'b.txt', { type: 'text/plain' }),
    ] } });

    expect(await screen.findByText('Uploading 1 of 2 files')).toBeVisible();
    expect(screen.getByRole('progressbar', { name: 'Files uploaded' })).toHaveAttribute('value', '1');
    await act(async () => finishUpload({
      import_ref: 'large-selection', state: 'running', admitted_files: 2, admitted_bytes: 2,
      through: 2, committed_rows: 1, committed_bytes: 1, sheet_id: 91,
      sheet_name: 'Files', cancel_requested: false, sealed: true, error: null,
      search_preparation_batch_id: null, search_preparation_status: 'pending',
    }));
    expect(await screen.findByText('Adding files to the sheet — 1 of 2 saved')).toBeVisible();
    expect(screen.getByRole('progressbar', { name: 'Files saved' })).toHaveAttribute('value', '1');
  });

  it('waits for pending preparation after files are committed, then offers Next', async () => {
    const completed = {
      import_ref: 'large-selection', state: 'completed', admitted_files: 2, admitted_bytes: 2,
      through: 2, committed_rows: 2, committed_bytes: 2, sheet_id: 91,
      sheet_name: 'Files', cancel_requested: false, sealed: true, error: null,
      search_preparation_batch_id: null, search_preparation_status: 'pending',
    };
    uploadFilesToImportSession.mockResolvedValue(completed);
    getImportSession.mockResolvedValueOnce(completed).mockResolvedValue({
      ...completed, search_preparation_status: 'available', search_preparation_batch_id: 'late-prep',
    });
    const onClose = vi.fn();
    renderDialog(true, vi.fn(), onClose);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files: [
      new File(['a'], 'a.txt', { type: 'text/plain' }),
      new File(['b'], 'b.txt', { type: 'text/plain' }),
    ] } });
    await waitFor(() => expect(getImportSession).toHaveBeenCalledTimes(1));
    expect(screen.getByText('Files saved. Checking text and search setup…')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Done' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: 'Next' }, { timeout: 2_500 }));
    await waitFor(() => expect(getSearchPreparation).toHaveBeenCalledWith('open-session-project', 'late-prep'));
    expect(onClose).not.toHaveBeenCalled();
  });

  it('settles a committed import with no preparation instead of spinning forever', async () => {
    uploadFilesToImportSession.mockResolvedValue({
      import_ref: 'large-selection', state: 'completed', admitted_files: 2, admitted_bytes: 2,
      through: 2, committed_rows: 2, committed_bytes: 2, sheet_id: 91,
      sheet_name: 'Files', cancel_requested: false, sealed: true, error: null,
      search_preparation_batch_id: null, search_preparation_status: 'none',
    });
    const onClose = vi.fn();
    renderDialog(true, vi.fn(), onClose);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files: [
      new File(['a'], 'first.txt', { type: 'text/plain' }),
      new File(['b'], 'second.txt', { type: 'text/plain' }),
    ] } });
    fireEvent.click(await screen.findByRole('button', { name: 'Done' }));
    expect(onClose).toHaveBeenCalledOnce();
    expect(screen.queryByText('Files saved. Checking text and search setup…')).not.toBeInTheDocument();
    expect(getSearchPreparation).not.toHaveBeenCalled();
    expect(getImportSession).not.toHaveBeenCalled();
  });

  it('keeps Next unavailable while an early preparation batch still has importing files', async () => {
    uploadFilesToImportSession.mockResolvedValue({
      import_ref: 'large-selection', state: 'running', admitted_files: 2, admitted_bytes: 2,
      through: 2, committed_rows: 1, committed_bytes: 1, sheet_id: 91,
      sheet_name: 'Files', cancel_requested: false, sealed: true, error: null,
      search_preparation_batch_id: 'early-prep', search_preparation_status: 'available',
    });
    let finishImport!: (value: unknown) => void;
    getImportSession.mockReturnValue(new Promise((resolve) => { finishImport = resolve; }));
    renderDialog(true);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files: [
      new File(['a'], 'a.txt', { type: 'text/plain' }),
      new File(['b'], 'b.txt', { type: 'text/plain' }),
    ] } });
    await waitFor(() => expect(getImportSession).toHaveBeenCalledOnce());
    expect(screen.getByText('Adding files to the sheet — 1 of 2 saved')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument();
    await act(async () => finishImport({
      import_ref: 'large-selection', state: 'completed', admitted_files: 2, admitted_bytes: 2,
      through: 2, committed_rows: 2, committed_bytes: 2, sheet_id: 91,
      sheet_name: 'Files', cancel_requested: false, sealed: true, error: null,
      search_preparation_batch_id: 'early-prep', search_preparation_status: 'available',
    }));
    expect(await screen.findByRole('button', { name: 'Next' })).toBeEnabled();
  });

  it('names a paused import and leaves recovery to the Imports notice', async () => {
    const paused = {
      import_ref: 'large-selection', state: 'paused', admitted_files: 2, admitted_bytes: 2,
      through: 2, committed_rows: 1, committed_bytes: 1, sheet_id: 91,
      sheet_name: 'Files', cancel_requested: false, sealed: false,
      error: 'Import processing failed. Retry the import to continue.',
      search_preparation_batch_id: null, search_preparation_status: 'pending',
    };
    uploadFilesToImportSession.mockResolvedValue(paused);
    getImportSession.mockResolvedValue(paused);
    renderDialog(true);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files: [
      new File(['a'], 'a.txt', { type: 'text/plain' }),
      new File(['b'], 'b.txt', { type: 'text/plain' }),
    ] } });

    expect(await screen.findByRole('heading', { name: 'Import paused' })).toBeVisible();
    expect(screen.getByText(/Import paused after 1 of 2 files/)).toBeVisible();
    expect(screen.getByRole('alert')).toHaveTextContent('Retry the import to continue');
    expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument();
  });

  it('names cancellation and explains that the global notice owns its decision', async () => {
    uploadFilesToImportSession.mockResolvedValue({
      import_ref: 'large-selection', state: 'cancelled', admitted_files: 2, admitted_bytes: 2,
      through: 2, committed_rows: 1, committed_bytes: 1, sheet_id: 91,
      sheet_name: 'Files', cancel_requested: true, sealed: true, error: null,
      search_preparation_batch_id: null, search_preparation_status: 'none',
    });
    renderDialog(true);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files: [
      new File(['a'], 'a.txt', { type: 'text/plain' }),
      new File(['b'], 'b.txt', { type: 'text/plain' }),
    ] } });

    expect(await screen.findByRole('heading', { name: 'Import cancelled' })).toBeVisible();
    expect(screen.getByText(/saved rows are waiting to be kept or removed/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Done' })).toBeEnabled();
    expect(getImportSession).not.toHaveBeenCalled();
  });

  it('surfaces a transient import follow-up failure and recovers on the next poll', async () => {
    uploadFilesToImportSession.mockResolvedValue({
      import_ref: 'large-selection', state: 'running', admitted_files: 2, admitted_bytes: 2,
      through: 2, committed_rows: 0, committed_bytes: 0, sheet_id: null,
      sheet_name: 'Files', cancel_requested: false, sealed: true, error: null,
      search_preparation_batch_id: null,
    });
    getImportSession
      .mockRejectedValueOnce(new Error('temporary status outage'))
      .mockResolvedValueOnce({
        import_ref: 'large-selection', state: 'completed', admitted_files: 2, admitted_bytes: 2,
        through: 2, committed_rows: 2, committed_bytes: 2, sheet_id: 91,
        sheet_name: 'Files', cancel_requested: false, sealed: true, error: null,
        search_preparation_batch_id: 'recovered-prep',
      });
    const onImported = vi.fn();
    renderDialog(true, onImported);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    fireEvent.change(screen.getByTestId('import-file-input'), {
      target: {
        files: [
          new File(['a'], 'first.txt', { type: 'text/plain' }),
          new File(['b'], 'second.txt', { type: 'text/plain' }),
        ],
      },
    });

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not refresh import progress. Retrying…',
    );
    await waitFor(() => expect(getImportSession).toHaveBeenCalledTimes(2), { timeout: 2_500 });
    fireEvent.click(await screen.findByTestId('import-files-next'));
    expect(await screen.findByTestId('search-preparation-receipt')).toHaveTextContent('Text ready for keyword search');
    expect(onImported).toHaveBeenCalledWith(91);
  });

  it('gates FollowTheMoney and uploads the selected format to the first materialized sheet', async () => {
    const onImported = vi.fn();
    const view = render(
      <ImportWorkspaceDialog
        open
        onClose={vi.fn()}
        onImported={onImported}
        onError={vi.fn()}
        onLaunchDownload={vi.fn()}
        ftmImportEnabled
      />,
    );

    const input = screen.getByRole('button', { name: 'Choose file' }).closest('.table-import-dropzone')?.querySelector('input');
    fireEvent.change(input!, { target: { files: [new File(['{"id":"x"}\n'], 'entities.jsonl', { type: 'application/json' })] } });
    fireEvent.click(screen.getByRole('button', { name: 'Use FollowTheMoney import' }));
    fireEvent.change(screen.getByTestId('import-ftm-dataset-name'), { target: { value: 'Entities' } });
    fireEvent.click(screen.getByRole('button', { name: 'Import' }));

    await waitFor(() => expect(importFollowTheMoney).toHaveBeenCalledWith(
      'open-session-project',
      expect.any(File),
      'Entities',
    ));
    await waitFor(() => expect(onImported).toHaveBeenCalledWith(88));
    expect(projectApi.getSheetData).toHaveBeenCalledWith('88', 0, 25);
    view.unmount();
  });

  it('does not offer FollowTheMoney when the catalog gate is absent and starts server content inspection', async () => {
    render(
      <ImportWorkspaceDialog open onClose={vi.fn()} onImported={vi.fn()} onError={vi.fn()} onLaunchDownload={vi.fn()} />,
    );
    const input = screen.getByRole('button', { name: 'Choose file' }).closest('.table-import-dropzone')?.querySelector('input');
    fireEvent.change(input!, { target: { files: [new File(['{"id":"x"}\n'], 'entities.jsonl', { type: 'application/json' })] } });
    expect(screen.queryByRole('button', { name: 'Use FollowTheMoney import' })).toBeNull();
    await waitFor(() => expect(createTableImportFromFile).toHaveBeenCalledWith(
      'open-session-project',
      expect.objectContaining({ name: 'entities.jsonl' }),
      expect.anything(),
    ));
    expect(screen.getAllByRole('button', { name: 'Checking…' })).not.toHaveLength(0);
  });

  it('routes a gated JSON/JSONL drop through FtM and refreshes its first sheet', async () => {
    const onImported = vi.fn();
    render(
      <ImportDropzone
        onOpenWorkspace={vi.fn()}
        onOpenTable={vi.fn()}
        onOpenBulk={vi.fn()}
        onImported={onImported}
        onError={vi.fn()}
        onLaunchDownload={vi.fn()}
        ftmImportEnabled
      />,
    );
    fireEvent.drop(screen.getByTestId('import-dropzone'), {
      dataTransfer: { files: [new File(['{"id":"x"}\n'], 'entities.jsonl', { type: 'application/json' })] },
    });
    await waitFor(() => expect(importFollowTheMoney).toHaveBeenCalledWith(
      'open-session-project', expect.any(File), undefined,
    ));
    await waitFor(() => expect(onImported).toHaveBeenCalledWith(88));
  });

  it('keeps partial bulk failure paths and reasons inert while landing on the first sheet', async () => {
    const unsafePath = 'mail/<img src="/bulk-failure-path" onerror="window.bulkFailurePathRan = true">.csv';
    const unsafeReason = '<a href="https://failures.example.test/details">details</a><script>window.bulkFailureReasonRan = true</script>';
    executeBulkImport.mockResolvedValue({
      created: [{
        id: 'inbox', kind: 'sheet', sheet_id: 41, sheet_name: 'Inbox', logical_paths: ['mail/inbox.csv'],
      }],
      failed: [{ logical_paths: [unsafePath], logical_paths_omitted: 2, error: unsafeReason }],
      failed_omitted: 3,
      first_sheet_id: 41,
      message: 'Imported 1 file; 4 files failed.',
      warnings: [],
    });
    const onImported = vi.fn();
    const view = renderDialog(true, onImported);

    fireEvent.click(screen.getByTestId('import-mode-files'));
    fireEvent.change(screen.getByTestId('import-file-input'), {
      target: {
        files: [
          new File(['subject\nHello\n'], 'inbox.csv', { type: 'text/csv' }),
          new File(['subject\nBroken\n'], 'broken.csv', { type: 'text/csv' }),
        ],
      },
    });
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Proposed outputs' })).toBeVisible());
    fireEvent.click(screen.getByLabelText('Import as separate sheets'));
    fireEvent.click(screen.getByRole('button', { name: 'Execute import' }));

    await waitFor(() => expect(onImported).toHaveBeenCalledWith(41));
    expect(screen.getByRole('alert')).toHaveTextContent('Imported 1 file; 4 files failed.');
    expect(screen.getByText(unsafePath)).toBeVisible();
    expect(screen.getByText(unsafeReason)).toBeVisible();
    expect(screen.getByText('2 additional paths were omitted.')).toBeVisible();
    expect(screen.getByText('3 additional failures were omitted.')).toBeVisible();
    expect(view.container.querySelector('img, script, a')).toBeNull();
    expect((window as Window & { bulkFailurePathRan?: boolean }).bulkFailurePathRan).toBeUndefined();
    expect((window as Window & { bulkFailureReasonRan?: boolean }).bulkFailureReasonRan).toBeUndefined();
  });

  it('paints preparation before scanning a large picker selection', async () => {
    renderDialog(true);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    const files = Array.from(
      { length: 4_096 },
      (_, index) => new File(['x'], `${index}.txt`, { type: 'text/plain' }),
    );

    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files } });

    expect(screen.getByText('Preparing selected files…')).toBeVisible();
    expect(createImportSession).not.toHaveBeenCalled();
    await waitFor(() => expect(createImportSession).toHaveBeenCalledWith(
      'open-session-project', { sheetName: 'files' },
    ));
    await waitFor(() => expect(uploadFilesToImportSession).toHaveBeenCalledWith(
      'open-session-project', expect.objectContaining({ import_ref: 'large-selection' }), files,
      expect.objectContaining({ onStatus: expect.any(Function) }),
    ));
  });

  it('does not dispatch a pending selection after the workspace closes', async () => {
    const view = renderDialog(true);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    const files = Array.from(
      { length: 4_096 },
      (_, index) => new File(['x'], `${index}.txt`, { type: 'text/plain' }),
    );
    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files } });
    expect(screen.getByText('Preparing selected files…')).toBeVisible();

    view.rerender(
      <ImportWorkspaceDialog
        open={false}
        onClose={vi.fn()}
        onImported={vi.fn()}
        onError={vi.fn()}
        onLaunchDownload={vi.fn()}
      />,
    );
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });

    expect(createImportSession).not.toHaveBeenCalled();
    expect(prepareImportFiles).not.toHaveBeenCalled();
    expect(uploadFilesToImportSession).not.toHaveBeenCalled();
  });

  it('does not dispatch a pending file selection after changing modes', async () => {
    renderDialog(true);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    const files = Array.from(
      { length: 4_096 },
      (_, index) => new File(['x'], `${index}.txt`, { type: 'text/plain' }),
    );
    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files } });
    fireEvent.click(screen.getByTestId('import-mode-csv'));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });

    expect(createImportSession).not.toHaveBeenCalled();
    expect(prepareImportFiles).not.toHaveBeenCalled();
    expect(uploadFilesToImportSession).not.toHaveBeenCalled();
  });

  it('hands an empty-project table drop to the staged table flow without legacy preview', () => {
    const onOpenTable = vi.fn();
    render(
      <ImportDropzone
        onOpenWorkspace={vi.fn()}
        onOpenTable={onOpenTable}
        onOpenBulk={vi.fn()}
        onImported={vi.fn()}
        onError={vi.fn()}
        onLaunchDownload={vi.fn()}
      />,
    );
    const file = new File(['name\nAda\n'], 'people.csv', { type: 'text/csv' });
    fireEvent.drop(screen.getByTestId('import-dropzone'), { dataTransfer: { files: [file] } });
    expect(onOpenTable).toHaveBeenCalledWith(file);
    expect(previewCsv).not.toHaveBeenCalled();
  });

  it('opens the empty-project workspace without passing the click event as an import mode', () => {
    const onOpenWorkspace = vi.fn();
    render(
      <ImportDropzone
        onOpenWorkspace={onOpenWorkspace}
        onOpenTable={vi.fn()}
        onOpenBulk={vi.fn()}
        onImported={vi.fn()}
        onError={vi.fn()}
        onLaunchDownload={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Open import' }));
    expect(onOpenWorkspace).toHaveBeenCalledWith();
  });

  it('does not dispatch a pending drop after the dropzone unmounts', async () => {
    const onOpenBulk = vi.fn();
    const view = render(
      <ImportDropzone
        onOpenWorkspace={vi.fn()}
        onOpenTable={vi.fn()}
        onOpenBulk={onOpenBulk}
        onImported={vi.fn()}
        onError={vi.fn()}
        onLaunchDownload={vi.fn()}
      />,
    );
    const files = Array.from(
      { length: 4_096 },
      (_, index) => new File(['x'], `${index}.txt`, { type: 'text/plain' }),
    );
    fireEvent.drop(screen.getByTestId('import-dropzone'), { dataTransfer: { files } });
    expect(screen.getByText('Preparing selected files…')).toBeVisible();

    view.unmount();
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    expect(onOpenBulk).not.toHaveBeenCalled();
    expect(createImportSession).not.toHaveBeenCalled();
    expect(uploadFilesToImportSession).not.toHaveBeenCalled();
  });

  it('removes the consumed bulk plan after execution fails', async () => {
    executeBulkImport.mockRejectedValue(new Error('Storage unavailable'));
    renderDialog(true);
    fireEvent.click(screen.getByTestId('import-mode-files'));
    fireEvent.change(screen.getByTestId('import-file-input'), {
      target: {
        files: [
          new File(['name\nAda\n'], 'inbox.csv', { type: 'text/csv' }),
          new File(['name\nGrace\n'], 'sent.csv', { type: 'text/csv' }),
        ],
      },
    });
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Proposed outputs' })).toBeVisible());
    fireEvent.click(screen.getByLabelText('Import as separate sheets'));
    fireEvent.click(screen.getByRole('button', { name: 'Execute import' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Storage unavailable'));
    expect(screen.queryByRole('button', { name: 'Execute import' })).toBeNull();
  });

  it('drops an externally closed in-flight bulk execution before reopening', async () => {
    let resolveExecution!: (value: {
      created: Array<{ id: string; kind: string; sheet_id: number; sheet_name: string; logical_paths: string[] }>;
      failed: unknown[];
      first_sheet_id: number;
      message: string;
      warnings: string[];
    }) => void;
    let executionSignal: AbortSignal | undefined;
    executeBulkImport.mockImplementation((_projectId, _planId, _decisions, options) => {
      executionSignal = options?.signal;
      return new Promise((resolve) => { resolveExecution = resolve; });
    });
    const onImported = vi.fn();
    const onClose = vi.fn();
    const view = renderDialog(true, onImported, onClose);

    fireEvent.click(screen.getByTestId('import-mode-files'));
    fireEvent.change(screen.getByTestId('import-file-input'), {
      target: {
        files: [
          new File(['subject\nHello\n'], 'inbox.csv', { type: 'text/csv' }),
          new File(['subject\nReply\n'], 'sent.csv', { type: 'text/csv' }),
        ],
      },
    });
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Proposed outputs' })).toBeVisible());
    fireEvent.click(screen.getByLabelText('Import as separate sheets'));
    fireEvent.click(screen.getByRole('button', { name: 'Execute import' }));
    await waitFor(() => expect(executeBulkImport).toHaveBeenCalledTimes(1));

    view.rerender(
      <ImportWorkspaceDialog
        open={false}
        onClose={onClose}
        onImported={onImported}
        onError={vi.fn()}
        onLaunchDownload={vi.fn()}
      />,
    );
    expect(onClose).not.toHaveBeenCalled();
    expect(executionSignal?.aborted).toBe(true);

    await act(async () => {
      resolveExecution({
        created: [{
          id: 'messages', kind: 'sheet', sheet_id: 17, sheet_name: 'Messages',
          logical_paths: ['inbox.csv', 'sent.csv'],
        }],
        failed: [],
        first_sheet_id: 17,
        message: 'Imported 2 files.',
        warnings: ['mail/bad.eml: stale warning'],
      });
    });
    expect(onImported).not.toHaveBeenCalled();

    view.rerender(
      <ImportWorkspaceDialog
        open
        onClose={onClose}
        onImported={onImported}
        onError={vi.fn()}
        onLaunchDownload={vi.fn()}
      />,
    );
    expect(screen.queryByRole('heading', { name: 'Proposed outputs' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Import as separate sheets')).not.toBeInTheDocument();
    expect(screen.queryByText('mail/bad.eml: stale warning')).not.toBeInTheDocument();
  });
});
