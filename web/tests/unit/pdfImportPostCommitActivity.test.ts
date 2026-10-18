import { GridCellKind } from '@glideapps/glide-data-grid';
import { describe, expect, it, vi } from 'vitest';

import type { ImportSessionStatus } from '../../src/api/open';
import {
  composeImportSearchActivity,
  importActivity,
  pendingImportRowCount,
  preferredImportActivity,
} from '../../src/components/useImportProgress';
import {
  abortImportSessionBrowserUpload,
  registerImportSessionUploadAbort,
} from '../../src/components/importProgressSession';
import type { SearchPreparationNotice } from '../../src/components/useSearchPreparationNotice';
import { buildCell } from '../../src/grid/cells';
import { aiMeta, columnDef, row } from '../support/domainFixtures';

function session(overrides: Partial<ImportSessionStatus> = {}): ImportSessionStatus {
  return {
    import_ref: 'import-00000000000000000000000000000000',
    state: 'completed',
    admitted_files: 12,
    admitted_bytes: 1_000,
    through: 12,
    committed_rows: 12,
    committed_bytes: 1_000,
    sheet_id: 7,
    sheet_name: 'PDFs',
    cancel_requested: false,
    sealed: true,
    error: null,
    search_preparation_status: 'none',
    search_preparation_batch_id: null,
    selected_total: 12,
    preparation_completed: 7,
    preparation_total: 12,
    ocr_completed: 3,
    ocr_total: 8,
    unreadable_total: 1,
    preparation_pending_confirmation: null,
    preparation_children: [{
      action_id: 'media.ocr',
      row_count: 8,
      completed_rows: 3,
      status: 'running',
      receipt_id: 'receipt',
      run_id: 42,
      job_id: 43,
    }],
    ...overrides,
  };
}

describe('PDF import post-commit activity', () => {
  it('derives adding/OCR counters and reuses the ordinary live run identity', () => {
    const activity = importActivity(session());

    expect(activity?.lines).toEqual([
      { key: 'adding', label: 'Adding rows', completed: 12, total: 12 },
      { key: 'ocr', label: 'OCR', completed: 3, total: 8 },
    ]);
    expect(activity?.liveRun).toMatchObject({
      runId: '42',
      sheetId: '7',
      actionKind: 'media.ocr',
      completedRows: 3,
      totalRows: 8,
      status: 'running',
    });
  });

  it('presents a terminal partial preparation child as failed instead of queued', () => {
    const activity = importActivity(session({
      state: 'running',
      preparation_children: [{
        action_id: 'media.read_pdf_text',
        row_count: 8,
        completed_rows: 3,
        status: 'partial',
        receipt_id: 'partial-receipt',
        run_id: 51,
        job_id: 52,
      }],
    }));

    expect(activity?.liveRun).toMatchObject({
      runId: '51',
      status: 'failed',
      completedRows: 3,
      totalRows: 8,
      failedRows: 5,
    });
  });

  it('combines import and existing index progress into one three-line entry', () => {
    const search = {
      activityKey: 'index:9',
      reviewBatchId: null,
      stage: 'index',
      tone: 'active',
      title: 'Preparing search',
      detail: 'Indexing search content',
      progress: null,
      details: [],
      pdfFiles: [
        { source_id: 'a', filename: 'a.pdf', completed: 2, total: 5 },
        { source_id: 'b', filename: 'b.pdf', completed: 3, total: 7 },
      ],
    } satisfies SearchPreparationNotice;

    const combined = composeImportSearchActivity(importActivity(session()), search);

    expect(combined?.notice.activityKey).toBe('import:import-00000000000000000000000000000000');
    expect(combined?.notice.title).toBe('Preparing search');
    expect(combined?.lines.map((line) => line.label)).toEqual([
      'Adding rows',
      'OCR',
      'Search index',
    ]);
    expect(combined?.lines[2]).toMatchObject({ completed: 5, total: 12 });
  });

  it('renders the OCR wait as UI-only pending data', () => {
    const column = columnDef({ type: 'text', ai: aiMeta() });
    const cell = buildCell(column, row({ [column.id]: null }), {
      pending: true,
      pendingLabel: 'Waiting for OCR',
    });

    expect(cell.kind).toBe(GridCellKind.Custom);
    expect(cell.copyData).toBe('');
    expect((cell as { data?: { label?: string } }).data?.label).toBe('Waiting for OCR');
  });

  it('adds placeholders only while the import is admitting rows', () => {
    expect(pendingImportRowCount(importActivity(session({
      state: 'running',
      committed_rows: 7,
    })))).toBe(5);
    expect(pendingImportRowCount(importActivity(session({
      state: 'paused',
      committed_rows: 7,
    })))).toBe(0);
    expect(pendingImportRowCount(importActivity(session({
      state: 'cancelled',
      committed_rows: 7,
    })))).toBe(0);
  });

  it('prefers a live import over an earlier recovery entry', () => {
    const paused = importActivity(session({
      import_ref: 'import-paused',
      state: 'paused',
      preparation_children: [],
      preparation_completed: 12,
    }));
    const live = importActivity(session({
      import_ref: 'import-live',
      state: 'running',
    }));

    expect(preferredImportActivity([paused!, live!])?.importRef).toBe('import-live');
  });

  it('prefers row admission over an earlier inactive preparation entry', () => {
    const inactive = importActivity(session({
      import_ref: 'import-inactive',
      preparation_children: [],
    }));
    const live = importActivity(session({
      import_ref: 'import-live',
      state: 'admitting',
    }));

    expect(preferredImportActivity([inactive!, live!])?.importRef).toBe('import-live');
  });

  it('aborts only the current browser upload registration', () => {
    const first = vi.fn();
    const second = vi.fn();
    const unregisterFirst = registerImportSessionUploadAbort('import-ref', first);
    registerImportSessionUploadAbort('import-ref', second);

    unregisterFirst();
    abortImportSessionBrowserUpload('import-ref');
    abortImportSessionBrowserUpload('import-ref');

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});
