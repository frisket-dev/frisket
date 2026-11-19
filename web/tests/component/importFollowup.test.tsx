// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ActionTemplate, ColumnDef, SheetDataPage } from '../../src/api/open';

const listImportSessions = vi.hoisted(() => vi.fn());
const cancelImportSession = vi.hoisted(() => vi.fn());

vi.mock('../../src/api/open', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/open')>(),
  listImportSessions,
  cancelImportSession,
}));

vi.mock('../../src/components/importWorkspace/tableImport', () => ({
  TableImportFlow: ({ onImported }: { onImported(sheetId: number): void }) => (
    <button type="button" onClick={() => onImported(41)}>Import rows</button>
  ),
}));

const projectApi = vi.hoisted(() => ({ getSheetData: vi.fn(), listSheets: vi.fn() }));

vi.mock('../../src/bind/useWorkspaceStores', async () => {
  const { createRouteStore } = await import('../../src/state/routeStore');
  const route = createRouteStore('project-1');
  return {
    useWorkspaceStores: () => ({ projectApi, route, chromePreferences: { projectId: 'project-1' } }),
  };
});

import { ImportWorkspaceDialog } from '../../src/components/importWorkspace/ImportWorkspaceDialog';
import {
  claimImportFollowup,
  expectImportFollowup,
  notifyImportSessionChanged,
} from '../../src/components/importProgressSession';
import { useImportProgress } from '../../src/components/useImportProgress';
import { useImportFollowup } from '../../src/components/importWorkspace/importFollowup';
import { ImportFollowupNotice } from '../../src/components/importWorkspace/ImportFollowupPrompts';

const tableSession = {
  import_ref: 'table-import-1',
  intake_kind: 'table',
  state: 'running',
  admitted_files: 1,
  admitted_bytes: 100,
  through: 1,
  committed_rows: 1,
  sheet_id: 41,
  sheet_name: 'Videos',
  cancel_requested: false,
  sealed: true,
  error: null,
  search_preparation_status: 'none',
  search_preparation_batch_id: null,
  selected_total: 2,
  preparation_completed: 0,
  preparation_total: 0,
  ocr_completed: 0,
  ocr_total: 0,
  unreadable_total: 0,
  preparation_children: [],
  reading_row_ids: [],
  waiting_row_ids: [],
  failed_pages: 0,
} as const;

const completed = { ...tableSession, state: 'completed', committed_rows: 2 } as const;

const videoColumn: ColumnDef = {
  id: '1',
  name: 'video_url',
  type: 'text',
  mediaDownloadCandidate: 'media.ytdlp_download',
};

const bodyColumn: ColumnDef = { id: '2', name: 'body', type: 'text' };

const nerTemplate: ActionTemplate = {
  kind: 'map.ner',
  name: 'Extract entities',
  description: 'Find people, organizations and places.',
  defaultPrompt: '',
  defaultFields: [],
  sourceRequirements: [
    { id: 'input_columns', label: 'Text', mode: 'columns', param: 'input_columns', accepted_column_types: ['text'] },
  ],
  engines: [{ id: 'spacy', label: 'spaCy', tier: 'local', available: true }],
};

function sheetPage(columns: ColumnDef[]): SheetDataPage {
  return { rows: [], total: 2, columns };
}


function Workspace() {
  const followup = useImportFollowup({ projectApi, nerTemplate });
  const progress = useImportProgress({ projectId: 'project-1', onFinished: followup.offer });
  const pending = followup.pending;
  return (
    <div>
      {pending && (
        <ImportFollowupNotice followup={pending.followup} onRun={followup.dismiss} onDismiss={followup.dismiss} />
      )}
      <button type="button" onClick={() => void progress.refresh()}>Refresh</button>
      {progress.activities.map((activity) => (
        <button key={activity.importRef} type="button" onClick={() => void progress.stop(activity)}>Stop import</button>
      ))}
    </div>
  );
}

async function settle() {
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
}

beforeEach(() => {
  vi.useFakeTimers();
  projectApi.getSheetData.mockResolvedValue(sheetPage([videoColumn]));
  projectApi.listSheets.mockResolvedValue([]);
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value() { this.open = true; },
  });
});

afterEach(() => {
  cleanup();
  claimImportFollowup('table-import-1');
  vi.resetAllMocks();
  vi.useRealTimers();
});

describe('a background import offers its follow-up when it finishes', () => {
  it('offers "Download media?" once, after the rows have landed, however often the sessions refresh', async () => {
    listImportSessions
      .mockResolvedValueOnce({ sessions: [tableSession] })
      .mockResolvedValue({ sessions: [completed] });
    expectImportFollowup('table-import-1');

    render(<Workspace />);
    await settle();
    expect(projectApi.getSheetData).not.toHaveBeenCalled();
    expect(screen.queryByTestId('media-download-prompt')).not.toBeInTheDocument();

    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    await settle();
    expect(screen.getByTestId('media-download-prompt')).toHaveTextContent('video_url');
    expect(projectApi.getSheetData).toHaveBeenCalledExactlyOnceWith('41', 0, 25);

    fireEvent.click(screen.getByTestId('media-download-dismiss'));
    act(() => notifyImportSessionChanged());
    await settle();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await settle();

    expect(listImportSessions.mock.calls.length).toBeGreaterThanOrEqual(4);
    expect(projectApi.getSheetData).toHaveBeenCalledOnce();
    expect(screen.queryByTestId('media-download-prompt')).not.toBeInTheDocument();
  });

  it('waits for PDF page reading before calling the import finished', async () => {
    const reading = { ...completed, intake_kind: 'files', preparation_total: 2, preparation_completed: 1 };
    listImportSessions
      .mockResolvedValueOnce({ sessions: [reading] })
      .mockResolvedValue({ sessions: [{ ...reading, preparation_completed: 2 }] });
    expectImportFollowup('table-import-1');

    render(<Workspace />);
    await settle();
    expect(projectApi.getSheetData).not.toHaveBeenCalled();

    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    await settle();
    expect(screen.getByTestId('media-download-prompt')).toBeInTheDocument();
    expect(projectApi.getSheetData).toHaveBeenCalledOnce();
  });

  it.each([
    ['cancelled', { ...tableSession, state: 'cancelled' }],
    ['stopped and kept', { ...tableSession, state: 'kept' }],
    ['completed with an error', { ...completed, error: 'The source could not be read.' }],
  ])('offers nothing for an import that was %s', async (_label, session) => {
    listImportSessions.mockResolvedValue({ sessions: [session] });
    expectImportFollowup('table-import-1');

    render(<Workspace />);
    await settle();
    await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });

    expect(projectApi.getSheetData).not.toHaveBeenCalled();
    expect(screen.queryByTestId('media-download-prompt')).not.toBeInTheDocument();
  });

  it('offers nothing after a Stop, even when the sessions refresh again', async () => {
    listImportSessions.mockResolvedValue({ sessions: [tableSession] });
    cancelImportSession.mockResolvedValue({ ...tableSession, state: 'cancelled' });
    expectImportFollowup('table-import-1');

    render(<Workspace />);
    await settle();
    fireEvent.click(screen.getByRole('button', { name: 'Stop import' }));
    await settle();
    listImportSessions.mockResolvedValue({ sessions: [{ ...tableSession, state: 'cancelled' }] });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await settle();

    expect(cancelImportSession).toHaveBeenCalledOnce();
    expect(projectApi.getSheetData).not.toHaveBeenCalled();
  });

  it('offers nothing for a finished import this tab did not commit, as after a reload', async () => {
    listImportSessions.mockResolvedValue({ sessions: [completed] });

    render(<Workspace />);
    await settle();
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });

    expect(projectApi.getSheetData).not.toHaveBeenCalled();
    expect(screen.queryByTestId('media-download-prompt')).not.toBeInTheDocument();
  });

  it('offers entity extraction when the finished sheet has text and no media links', async () => {
    projectApi.getSheetData.mockResolvedValue(sheetPage([bodyColumn]));
    listImportSessions.mockResolvedValue({ sessions: [completed] });
    expectImportFollowup('table-import-1');

    render(<Workspace />);
    await settle();

    await settle();
    expect(screen.getByTestId('ner-import-prompt')).toHaveTextContent('body');
    expect(screen.queryByTestId('media-download-prompt')).not.toBeInTheDocument();
  });

  it('leaves a table import to the completion path: the dialog closes on commit without classifying', async () => {
    const onImported = vi.fn();
    const onClose = vi.fn();
    render(
      <ImportWorkspaceDialog
        open
        entryMode="csv"
        onClose={onClose}
        onImported={onImported}
        onError={vi.fn()}
        onLaunchDownload={vi.fn()}
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole('button', { name: 'Import rows' }));
    await settle();

    expect(onImported).toHaveBeenCalledExactlyOnceWith(41);
    expect(onClose).toHaveBeenCalledOnce();
    expect(projectApi.getSheetData).not.toHaveBeenCalled();
    expect(screen.queryByTestId('media-download-prompt')).not.toBeInTheDocument();
  });
});
