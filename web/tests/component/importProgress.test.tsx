// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const listImportSessions = vi.hoisted(() => vi.fn());
const cancelImportSession = vi.hoisted(() => vi.fn());
const resolveImportSession = vi.hoisted(() => vi.fn());
const resumeImportSession = vi.hoisted(() => vi.fn());
const uploadFilesToImportSession = vi.hoisted(() => vi.fn());

vi.mock('../../src/api/open', () => ({
  listImportSessions,
  cancelImportSession,
  resolveImportSession,
  resumeImportSession,
  uploadFilesToImportSession,
  snapshotImportFiles: (files: FileList | null) => Array.from(files ?? []),
  importFileLogicalPath: (file: File) => file.name,
}));

import {
  notifyImportSessionChanged,
  registerImportSessionUploadAbort,
} from '../../src/components/importProgressSession';
import {
  useImportProgress,
  type ImportActivity,
  type ImportProgressChange,
} from '../../src/components/useImportProgress';

const running = {
  import_ref: 'import-1',
  state: 'running',
  admitted_files: 10,
  admitted_bytes: 100,
  through: 10,
  committed_rows: 3,
  committed_bytes: 30,
  sheet_id: 9,
  sheet_name: 'Files',
  cancel_requested: false,
  sealed: true,
  error: null,
  search_preparation_status: 'none',
  search_preparation_batch_id: null,
  selected_total: 10,
  preparation_completed: 0,
  preparation_total: 2,
  ocr_completed: 0,
  ocr_total: 2,
  unreadable_total: 0,
  preparation_pending_confirmation: null,
  preparation_children: [],
} as const;

function Harness({ onChanged }: { onChanged?: (change: ImportProgressChange) => void } = {}) {
  const controller = useImportProgress({ projectId: 'project-1', onChanged });
  return (
    <div>
      {controller.activities.map((activity: ImportActivity) => (
        <section key={activity.importRef} data-testid="activity">
          {activity.lines.map((line) => (
            <span key={line.key}>{line.label}: {line.completed}/{line.total}</span>
          ))}
          <button type="button" onClick={() => controller.resume(activity)}>Resume</button>
          <button type="button" onClick={() => void controller.stop(activity)}>Stop remaining</button>
          <button type="button" onClick={() => controller.keep(activity)}>Keep what's added</button>
        </section>
      ))}
    </div>
  );
}

beforeEach(() => {
  listImportSessions.mockResolvedValue({ sessions: [running] });
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  vi.useRealTimers();
});

describe('persistent import progress controller', () => {
  it('uses soft refresh only for changing live table rows and hard refreshes terminal state', async () => {
    vi.useFakeTimers();
    const changed = vi.fn();
    const table = {
      ...running,
      intake_kind: 'table',
      selected_total: 120,
      committed_rows: 10,
      processed_rows: 10,
      preparation_total: 0,
      ocr_total: 0,
    } as const;
    listImportSessions
      .mockResolvedValueOnce({ sessions: [table] })
      .mockResolvedValueOnce({ sessions: [table] })
      .mockResolvedValueOnce({ sessions: [{ ...table, committed_rows: 20, processed_rows: 20 }] })
      .mockResolvedValueOnce({ sessions: [{ ...table, state: 'completed', committed_rows: 120, processed_rows: 120 }] });

    render(<Harness onChanged={changed} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(changed).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(changed).toHaveBeenLastCalledWith({ sheetId: null, refreshData: false });
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(changed).toHaveBeenLastCalledWith({ sheetId: null, refreshData: true });
    expect(changed).toHaveBeenCalledTimes(2);
  });

  it('hard-refreshes PDF preparation progress', async () => {
    vi.useFakeTimers();
    const changed = vi.fn();
    listImportSessions
      .mockResolvedValueOnce({ sessions: [running] })
      .mockResolvedValueOnce({ sessions: [{ ...running, preparation_completed: 1 }] });

    render(<Harness onChanged={changed} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });

    expect(changed).toHaveBeenCalledOnce();
    expect(changed).toHaveBeenCalledWith({ sheetId: null, refreshData: true });
  });

  it('does not let an older poll hide a newly committed import', async () => {
    vi.useFakeTimers();
    let finishOldPoll!: (value: unknown) => void;
    listImportSessions
      .mockResolvedValueOnce({ sessions: [running] })
      .mockImplementationOnce(() => new Promise((resolve) => { finishOldPoll = resolve; }))
      .mockResolvedValue({ sessions: [{ ...running, committed_rows: 7 }] });
    render(<Harness />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(listImportSessions).toHaveBeenCalledTimes(2);
    act(() => notifyImportSessionChanged());
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText('Adding rows: 7/10')).toBeInTheDocument();
    await act(async () => { finishOldPoll({ sessions: [running] }); });
    expect(screen.getByText('Adding rows: 7/10')).toBeInTheDocument();
  });

  it('keeps a stopped table import visible immediately after Stop', async () => {
    const table = { ...running, intake_kind: 'table', preparation_total: 0, ocr_total: 0 };
    const changed = vi.fn();
    listImportSessions.mockResolvedValue({ sessions: [table] });
    cancelImportSession.mockResolvedValue({ ...table, state: 'cancelled' });
    resolveImportSession.mockResolvedValue({ ...table, state: 'kept' });
    render(<Harness onChanged={changed} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Stop remaining' }));
    await waitFor(() => expect(resolveImportSession).toHaveBeenCalled());
    expect(screen.getByText('Adding rows: 3/10')).toBeInTheDocument();
    expect(changed).toHaveBeenCalledWith({ sheetId: 9, refreshData: true });
  });

  it('keeps a completed import visible while ordinary PDF preparation remains', async () => {
    listImportSessions.mockResolvedValue({
      sessions: [{ ...running, state: 'completed', committed_rows: 10 }],
    });

    render(<Harness />);

    expect(await screen.findByText('Adding rows: 10/10')).toBeInTheDocument();
    expect(screen.getByText('OCR: 0/2')).toBeInTheDocument();
  });

  it('stops the browser upload before canonical cancel and keep', async () => {
    const order: string[] = [];
    registerImportSessionUploadAbort('import-1', () => { order.push('abort'); });
    cancelImportSession.mockImplementation(async () => {
      order.push('cancel');
      return { ...running, state: 'cancelled' };
    });
    resolveImportSession.mockImplementation(async () => {
      order.push('keep');
      return { ...running, state: 'kept' };
    });

    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: 'Stop remaining' }));

    await waitFor(() => expect(resolveImportSession).toHaveBeenCalledWith(
      'project-1',
      'import-1',
      'keep',
    ));
    expect(order).toEqual(['abort', 'cancel', 'keep']);
  });

  it('resumes a sealed paused session through the canonical endpoint', async () => {
    const paused = { ...running, state: 'paused' };
    listImportSessions.mockResolvedValue({ sessions: [paused] });
    resumeImportSession.mockResolvedValue({ ...running, state: 'running' });

    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: 'Resume' }));

    await waitFor(() => expect(resumeImportSession).toHaveBeenCalledWith(
      'project-1',
      'import-1',
    ));
  });

  it('keeps polling while durable preparation is incomplete', async () => {
    vi.useFakeTimers();
    listImportSessions.mockResolvedValue({ sessions: [{ ...running, state: 'completed' }] });

    render(<Harness />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });

    expect(listImportSessions).toHaveBeenCalledTimes(2);
  });
});
