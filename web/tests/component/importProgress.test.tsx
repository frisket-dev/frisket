// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
  ImportProgress,
  rememberImportSessionFiles,
  setImportSessionBrowserUploading,
} from '../../src/components/ImportProgress';

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
    rememberImportSessionFiles('import-1', [file], ['folder/one.pdf']);
    setImportSessionBrowserUploading('project-1', 'import-1', true);
    render(<ImportProgress projectId="project-1" onOpenSheet={vi.fn()} />);

    expect(await screen.findByText(/Uploading files/)).toBeInTheDocument();
    expect(screen.queryByText(/Upload was interrupted/)).not.toBeInTheDocument();
    setImportSessionBrowserUploading('project-1', 'import-1', false);
    await waitFor(() => expect(screen.getByText(/Upload was interrupted/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Reselect files' }));
    expect(screen.getByLabelText('Reselect import files')).toHaveAttribute('webkitdirectory');
  });
});
