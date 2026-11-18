// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const listImportSessions = vi.hoisted(() => vi.fn());
const cancelImportSession = vi.hoisted(() => vi.fn());
const resolveImportSession = vi.hoisted(() => vi.fn());
const resumeImportSession = vi.hoisted(() => vi.fn());
const uploadFilesToImportSession = vi.hoisted(() => vi.fn());
const estimateImportPreparationRetry = vi.hoisted(() => vi.fn());
const retryImportPreparation = vi.hoisted(() => vi.fn());

vi.mock('../../src/api/open', () => ({
  listImportSessions,
  cancelImportSession,
  resolveImportSession,
  resumeImportSession,
  uploadFilesToImportSession,
  estimateImportPreparationRetry,
  retryImportPreparation,
  snapshotImportFiles: (files: FileList | null) => Array.from(files ?? []),
  importFileLogicalPath: (file: File) => file.name,
}));

import { notifyImportSessionChanged } from '../../src/components/importProgressSession';
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
  preparation_children: [],
  reading_row_ids: [],
  waiting_row_ids: [],
  failed_pages: 0,
} as const;

const failedPages = {
  ...running,
  state: 'completed',
  committed_rows: 10,
  preparation_completed: 1,
  ocr_completed: 1,
  failed_pages: 1,
  error: 'Some PDF pages could not be prepared.',
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
          <button type="button" onClick={() => controller.keep(activity)}>Keep completed work</button>
          <button type="button" onClick={() => controller.remove(activity)}>Remove imported work</button>
          {activity.canRetry && (
            <button type="button" onClick={() => controller.retryFailedPages(activity)}>Retry failed pages</button>
          )}
        </section>
      ))}
      {controller.retryConsent && (
        <button type="button" onClick={() => controller.confirmRetry(controller.retryConsent!)}>
          Consent to {controller.retryConsent.pages} pages
        </button>
      )}
      {controller.message && <p role="alert">{controller.message}</p>}
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
    await waitFor(() => expect(cancelImportSession).toHaveBeenCalled());
    expect(resolveImportSession).not.toHaveBeenCalled();
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

  it('cancels and waits for an explicit Keep choice', async () => {
    const order: string[] = [];
    cancelImportSession.mockImplementation(async () => {
      order.push('cancel');
      const cancelled = { ...running, state: 'cancelled' };
      listImportSessions.mockResolvedValue({ sessions: [cancelled] });
      return cancelled;
    });
    resolveImportSession.mockImplementation(async () => {
      order.push('keep');
      return { ...running, state: 'kept' };
    });

    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: 'Stop remaining' }));

    await waitFor(() => expect(cancelImportSession).toHaveBeenCalled());
    expect(resolveImportSession).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Keep completed work' }));
    await waitFor(() => expect(resolveImportSession).toHaveBeenCalledWith(
      'project-1',
      'import-1',
      'keep',
    ));
    expect(order).toEqual(['cancel', 'keep']);
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

  it('retries failed pages directly when the estimate needs no consent', async () => {
    listImportSessions.mockResolvedValue({ sessions: [failedPages] });
    estimateImportPreparationRetry.mockResolvedValue({
      planToken: 'retry:plan', pages: 1, interruptedPages: 0,
      estimate: { cost: 0, rows: 1, requires_confirmation: false },
    });
    retryImportPreparation.mockImplementation(async () => {
      const retried = { ...failedPages, failed_pages: 0, error: null, waiting_row_ids: [4] };
      listImportSessions.mockResolvedValue({ sessions: [retried] });
      return retried;
    });

    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry failed pages' }));

    await waitFor(() => expect(retryImportPreparation).toHaveBeenCalledWith(
      'project-1', 'import-1', 'retry:plan', null,
    ));
    expect(estimateImportPreparationRetry).toHaveBeenCalledWith('project-1', 'import-1');
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Retry failed pages' }))
      .not.toBeInTheDocument());
  });

  it('retries failed pages only after consent to that exact estimate', async () => {
    listImportSessions.mockResolvedValue({ sessions: [failedPages] });
    estimateImportPreparationRetry.mockResolvedValue({
      planToken: 'retry:paid', pages: 3, interruptedPages: 0,
      estimate: { cost: 0.3, rows: 3, requires_confirmation: true, promise_set_hash: 'hash-3' },
    });
    retryImportPreparation.mockResolvedValue({ ...failedPages, failed_pages: 0, error: null });

    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry failed pages' }));
    const consent = await screen.findByRole('button', { name: 'Consent to 3 pages' });
    expect(retryImportPreparation).not.toHaveBeenCalled();
    fireEvent.click(consent);

    await waitFor(() => expect(retryImportPreparation).toHaveBeenCalledWith(
      'project-1', 'import-1', 'retry:paid', 'hash-3',
    ));
    expect(screen.queryByRole('button', { name: 'Consent to 3 pages' })).not.toBeInTheDocument();
  });

  it('asks before reading interrupted pages again even under the allowance', async () => {
    listImportSessions.mockResolvedValue({ sessions: [failedPages] });
    estimateImportPreparationRetry.mockResolvedValue({
      planToken: 'retry:interrupted', pages: 1, interruptedPages: 1,
      estimate: { cost: 0.01, rows: 1, requires_confirmation: false },
    });
    retryImportPreparation.mockResolvedValue({ ...failedPages, failed_pages: 0, error: null });

    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry failed pages' }));
    const consent = await screen.findByRole('button', { name: 'Consent to 1 pages' });
    expect(retryImportPreparation).not.toHaveBeenCalled();
    fireEvent.click(consent);

    await waitFor(() => expect(retryImportPreparation).toHaveBeenCalledWith(
      'project-1', 'import-1', 'retry:interrupted', null,
    ));
  });

  it('shows a changed retry estimate and lets the person estimate again', async () => {
    listImportSessions.mockResolvedValue({ sessions: [failedPages] });
    estimateImportPreparationRetry.mockResolvedValue({
      planToken: 'retry:stale', pages: 1, interruptedPages: 0,
      estimate: { cost: 0, rows: 1, requires_confirmation: false },
    });
    retryImportPreparation.mockRejectedValueOnce(
      new Error('The failed pages changed; review the retry estimate again'),
    );

    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry failed pages' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('The failed pages changed');
    fireEvent.click(screen.getByRole('button', { name: 'Retry failed pages' }));
    await waitFor(() => expect(estimateImportPreparationRetry).toHaveBeenCalledTimes(2));
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

 it('removes cancelled work only after the explicit choice', async () => {
    listImportSessions.mockResolvedValue({ sessions: [{ ...running, state: 'cancelled' }] });
    resolveImportSession.mockResolvedValue({ ...running, state: 'removed' });
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: 'Remove imported work' }));
    await waitFor(() => expect(resolveImportSession).toHaveBeenCalledWith('project-1', 'import-1', 'remove'));
    expect(cancelImportSession).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByTestId('activity')).not.toBeInTheDocument());
 });
