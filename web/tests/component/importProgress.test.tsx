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
}));

import {
  rememberImportSessionFiles,
  setImportSessionBrowserUploading,
} from '../../src/components/importProgressSession';
import { ImportProgress } from '../../src/components/ImportProgress';

const running = {
  import_ref: 'import-1', state: 'running', admitted_files: 10, admitted_bytes: 100,
  through: 10, committed_rows: 3, committed_bytes: 30, sheet_id: 9,
  sheet_name: 'Files', cancel_requested: false, sealed: true, error: null,
};

beforeEach(() => {
  listImportSessions.mockResolvedValue({ sessions: [running] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe('persistent import progress', () => {
  it('restores running progress, opens committed rows read-only, and offers cancellation', async () => {
    const onOpenSheet = vi.fn();
    cancelImportSession.mockResolvedValue({ ...running, state: 'cancelling', cancel_requested: true });
    render(<ImportProgress projectId="project-1" onOpenSheet={onOpenSheet} />);

    expect(await screen.findByText(/3 of 10 rows added/)).toBeInTheDocument();
    expect(screen.getByText(/read-only until import settles/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Open sheet' }));
    expect(onOpenSheet).toHaveBeenCalledWith(9);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(cancelImportSession).toHaveBeenCalledWith('project-1', 'import-1'));
  });

  it('keeps cancelled rows by default and makes removal explicit', async () => {
    const cancelled = { ...running, state: 'cancelled', committed_rows: 3 };
    listImportSessions.mockResolvedValue({ sessions: [cancelled] });
    resolveImportSession.mockImplementation(async (_project: string, _ref: string, decision: string) => ({
      ...cancelled, state: decision === 'keep' ? 'kept' : 'removed',
    }));
    render(<ImportProgress projectId="project-1" onOpenSheet={vi.fn()} />);

    expect(await screen.findByText(/3 rows retained/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Keep' }));
    await waitFor(() => expect(resolveImportSession).toHaveBeenCalledWith('project-1', 'import-1', 'keep'));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Remove added rows' })).not.toBeInTheDocument());
  });

  it('distinguishes a live folder upload from an interrupted selection', async () => {
    const admitting = { ...running, state: 'admitting', committed_rows: 0, sheet_id: null, sealed: false };
    listImportSessions.mockResolvedValue({ sessions: [admitting] });
    const file = new File(['one'], 'one.pdf');
    Object.defineProperty(file, 'webkitRelativePath', { value: 'folder/one.pdf' });
    await rememberImportSessionFiles('import-1', [file], ['folder/one.pdf']);
    setImportSessionBrowserUploading('import-1', true);
    render(<ImportProgress projectId="project-1" onOpenSheet={vi.fn()} />);

    expect(await screen.findByText(/Uploading files/)).toBeInTheDocument();
    expect(screen.queryByText(/Upload was interrupted/)).not.toBeInTheDocument();
    setImportSessionBrowserUploading('import-1', false);
    await waitFor(() => expect(screen.getByText(/Upload was interrupted/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Reselect files' }));
    expect(screen.getByLabelText('Reselect import files')).toHaveAttribute('webkitdirectory');
  });

  it('keeps polling a paused session through an automatic retry to completion', async () => {
    vi.useFakeTimers();
    const paused = { ...running, state: 'paused', committed_rows: 3 };
    const completed = { ...running, state: 'completed', committed_rows: 10 };
    listImportSessions
      .mockResolvedValueOnce({ sessions: [paused] })
      .mockResolvedValue({ sessions: [completed] });
    render(<ImportProgress projectId="project-1" onOpenSheet={vi.fn()} />);

    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByTestId('import-progress-paused')).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(screen.getByTestId('import-progress-completed')).toHaveTextContent('10 rows added');
    expect(listImportSessions).toHaveBeenCalledTimes(2);
  });

  it('allows cancellation while a resumed browser upload chunk is pending', async () => {
    const admitting = {
      ...running, state: 'admitting', through: 0, committed_rows: 0, sheet_id: null, sealed: false,
    };
    listImportSessions.mockResolvedValue({ sessions: [admitting] });
    cancelImportSession.mockResolvedValue({ ...admitting, state: 'cancelling', cancel_requested: true });
    let finishUpload!: () => void;
    uploadFilesToImportSession.mockImplementation(() => new Promise((resolve) => {
      finishUpload = () => resolve({ ...admitting, state: 'running', sealed: true });
    }));
    const file = new File(['one'], 'one.pdf');
    await rememberImportSessionFiles('import-1', [file], ['one.pdf']);
    render(<ImportProgress projectId="project-1" onOpenSheet={vi.fn()} />);

    await screen.findByText(/Upload was interrupted/);
    fireEvent.click(screen.getByRole('button', { name: 'Reselect files' }));
    fireEvent.change(screen.getByLabelText('Reselect import files'), { target: { files: [file] } });
    await waitFor(() => expect(uploadFilesToImportSession).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(cancelImportSession).toHaveBeenCalledWith('project-1', 'import-1'));
    await act(async () => { finishUpload(); });
  });
});
