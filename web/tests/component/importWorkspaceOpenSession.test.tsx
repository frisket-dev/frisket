// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const planBulkImport = vi.hoisted(() => vi.fn());
const executeBulkImport = vi.hoisted(() => vi.fn());
const importFollowTheMoney = vi.hoisted(() => vi.fn());
const importCsv = vi.hoisted(() => vi.fn());
const previewCsv = vi.hoisted(() => vi.fn());
const projectApi = vi.hoisted(() => ({ getSheetData: vi.fn(), listSheets: vi.fn() }));

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    planBulkImport,
    executeBulkImport,
    importFollowTheMoney,
    importCsv,
    previewCsv,
  };
});

vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({
    projectApi,
    chromePreferences: { projectId: 'open-session-project' },
  }),
}));

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
      onClose={onClose}
      onImported={onImported}
      onError={vi.fn()}
      onLaunchDownload={vi.fn()}
    />,
  );
}

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value() { this.open = true; },
  });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', {
    configurable: true,
    value() { this.open = false; },
  });
  planBulkImport.mockResolvedValue(plan);
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
  projectApi.getSheetData.mockResolvedValue({ columns: [], total: 0 });
  projectApi.listSheets.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('ImportWorkspaceDialog controlled open session', () => {
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

    expect(screen.getByTestId('import-file-format')).toHaveTextContent('FollowTheMoney');
    fireEvent.change(screen.getByTestId('import-file-format'), { target: { value: 'ftm' } });
    fireEvent.change(screen.getByTestId('import-ftm-dataset-name'), { target: { value: 'Entities' } });
    fireEvent.change(screen.getByTestId('import-csv-input'), {
      target: { files: [new File(['{"id":"x"}\n'], 'entities.jsonl', { type: 'application/json' })] },
    });

    await waitFor(() => expect(importFollowTheMoney).toHaveBeenCalledWith(
      'open-session-project',
      expect.any(File),
      'Entities',
    ));
    await waitFor(() => expect(onImported).toHaveBeenCalledWith(88));
    expect(projectApi.getSheetData).toHaveBeenCalledWith('88', 0, 25);
    view.unmount();
  });

  it('does not offer FollowTheMoney when the catalog gate is absent', () => {
    renderDialog(true);
    expect(screen.getByTestId('import-file-format')).not.toHaveTextContent('FollowTheMoney');
  });

  it('offers only root sheets for append and rotates the append key for a replacement file', async () => {
    projectApi.listSheets.mockResolvedValue([
      // Imports record their creation op, but no parent sheet: this root
      // remains an append destination.
      {
        id: '10', name: 'Imported root', rowCount: 1,
        columns: [{ id: 'name', name: 'name', type: 'text' }],
        citedColumnIds: [], annotatedTextColumnIds: [],
      },
      {
        id: '11', name: 'Derived child', rowCount: 1,
        columns: [{ id: 'name', name: 'name', type: 'text' }],
        parent: { sheetId: '10', sheetName: 'Imported root', viaAction: 'derive.rows' },
        citedColumnIds: [], annotatedTextColumnIds: [],
      },
    ]);
    renderDialog(true);
    const first = new File(['name\nAda\n'], 'first.csv', { type: 'text/csv' });
    fireEvent.change(screen.getByTestId('import-csv-input'), { target: { files: [first] } });

    const destination = await screen.findByLabelText('Destination');
    expect(destination).toHaveTextContent('Imported root');
    expect(destination).not.toHaveTextContent('Derived child');
    fireEvent.change(destination, { target: { value: '10' } });
    const confirm = screen.getByTestId('import-csv-confirm');
    await waitFor(() => expect(confirm).toBeEnabled());

    fireEvent.click(confirm);
    await waitFor(() => expect(importCsv).toHaveBeenCalledTimes(1));
    const firstKey = importCsv.mock.calls[0][4];
    fireEvent.click(confirm);
    await waitFor(() => expect(importCsv).toHaveBeenCalledTimes(2));
    expect(importCsv.mock.calls[1][4]).toBe(firstKey);

    const replacement = new File(['name\nBea\n'], 'replacement.csv', { type: 'text/csv' });
    fireEvent.change(screen.getByTestId('import-csv-input'), { target: { files: [replacement] } });
    await waitFor(() => expect(previewCsv).toHaveBeenCalledWith(
      'open-session-project', replacement, undefined, expect.anything(),
    ));
    fireEvent.click(screen.getByTestId('import-csv-confirm'));
    await waitFor(() => expect(importCsv).toHaveBeenCalledTimes(3));
    expect(importCsv.mock.calls[2][4]).not.toBe(firstKey);
  });

  it('routes a gated JSON/JSONL drop through FtM and refreshes its first sheet', async () => {
    const onImported = vi.fn();
    render(
      <ImportDropzone
        onOpenWorkspace={vi.fn()}
        onOpenCsv={vi.fn()}
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
