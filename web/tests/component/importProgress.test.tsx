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
  registerImportSessionUploadAbort,
} from '../../src/components/importProgressSession';
import {
  useImportProgress,
  type ImportActivity,
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

function Harness() {
  const controller = useImportProgress({ projectId: 'project-1' });
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
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe('persistent import progress controller', () => {
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
