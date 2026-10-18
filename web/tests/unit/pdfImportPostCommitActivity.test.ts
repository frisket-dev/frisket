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
  it('derives adding/native/OCR counters and reuses the ordinary live run identity', () => {
    const activity = importActivity(session());

    expect(activity?.lines).toEqual([
      { key: 'adding', label: 'Adding rows', completed: 12, total: 12, complete: true },
      {
        key: 'native', label: 'Text layer', completed: 4, total: 4,
        complete: false, detail: '4 of 4 · needs attention',
      },
      { key: 'ocr', label: 'OCR', completed: 3, total: 8, complete: false },
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

  it('waits for authoritative admission completion before marking added rows done', () => {
    expect(importActivity(session({ state: 'running' }))?.lines[0].complete).toBe(false);
    expect(importActivity(session({
      state: 'kept',
      committed_rows: 7,
    }))?.lines[0].complete).toBe(false);
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
    expect(activity?.lines.find((line) => line.key === 'native')).toMatchObject({
      complete: false,
      detail: '4 of 4 · needs attention',
    });
  });

  it('keeps a completed import refusal visible with its durable reason', () => {
    const activity = importActivity(session({
      preparation_completed: 12,
      error: 'paid PDF OCR price or route changed',
      preparation_children: [{
        action_id: 'media.ocr',
        row_count: 8,
        completed_rows: 8,
        status: 'failed',
        receipt_id: null,
        run_id: null,
        job_id: null,
      }],
    }));
    const combined = composeImportSearchActivity(activity, null);

    expect(activity).not.toBeNull();
    expect(activity?.liveRun).toBeNull();
    expect(combined?.notice).toMatchObject({
      stage: 'failed',
      tone: 'error',
      title: 'PDF preparation failed',
      detail: 'paid PDF OCR price or route changed',
    });
  });

  it('combines import and existing index progress into one four-line entry', () => {
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
      pdfFilesTruncated: true,
    } satisfies SearchPreparationNotice;

    const combined = composeImportSearchActivity(importActivity(session()), search);

    expect(combined?.notice.activityKey).toBe('import:import-00000000000000000000000000000000');
    expect(combined?.notice.title).toBe('Preparing search');
    expect(combined?.notice.pdfFiles).toEqual(search.pdfFiles);
    expect(combined?.notice.pdfFilesTruncated).toBe(true);
    expect(combined?.lines.map((line) => line.label)).toEqual([
      'Adding rows',
      'Text layer',
      'OCR',
      'Search index',
    ]);
    expect(combined?.lines[3]).toMatchObject({ completed: 5, total: 12 });
  });

  it('keeps unknown search progress indeterminate instead of inventing a completed count', () => {
    const combined = composeImportSearchActivity(importActivity(session()), null);

    expect(combined?.lines[3]).toMatchObject({
      key: 'search-index',
      completed: null,
      total: null,
      complete: false,
      detail: 'Waiting for search activity',
    });
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

describe('table import progress', () => {
  it('does not leave placeholders for processed duplicates', () => {
    const activity = importActivity(session({
      intake_kind: 'table', state: 'running', selected_total: 100,
      processed_rows: 50, committed_rows: 25, skipped_duplicates: 25,
    }));
    expect(pendingImportRowCount(activity)).toBe(50);
  });

  it('keeps an estimate at least as large as the rows already processed', () => {
    const activity = importActivity(session({
      intake_kind: 'table', state: 'running', selected_total: null,
      estimated_rows: 100, processed_rows: 120, committed_rows: 110,
    }));
    const combined = composeImportSearchActivity(activity, null);
    expect(combined?.lines[0]).toMatchObject({ completed: 120, total: 120 });
    expect(activity?.lines[0].completed).toBe(110);
    expect(pendingImportRowCount(activity)).toBe(0);
  });

  it('keeps an unknown row total indeterminate and shows actual processed rows', () => {
    const activity = importActivity(session({
      intake_kind: 'table', state: 'running', selected_total: null,
      estimated_rows: null, processed_rows: 500, committed_rows: 475,
      skipped_duplicates: 25, source_label: 'contributions.csv',
      preparation_children: [], preparation_total: 0, preparation_completed: 0,
      ocr_total: 0, ocr_completed: 0, unreadable_total: 0,
      sheet_name: 'Contributions',
    }));
    const combined = composeImportSearchActivity(activity, null);
    expect(combined?.notice.title).toBe('Importing contributions.csv');
    expect(combined?.notice.detail).toContain('475 rows added');
    expect(combined?.notice.detail).toContain('25 duplicates skipped');
    expect(combined?.lines[0]).toMatchObject({ completed: 500, total: null, complete: false });
    expect(pendingImportRowCount(activity)).toBe(0);
  });
  it('labels retained partial rows as stopped rather than complete', () => {
    const activity = importActivity(session({
      intake_kind: 'table', state: 'kept', selected_total: 100,
      processed_rows: 20, committed_rows: 20,
    }));
    const combined = composeImportSearchActivity(activity, null);
    expect(combined?.notice.tone).toBe('warning');
    expect(combined?.lines[0]).toMatchObject({ complete: false, detail: '20 added · stopped' });
  });
});
