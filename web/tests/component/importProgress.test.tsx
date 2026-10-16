// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const getSearchPreparation = vi.hoisted(() => vi.fn());
const getSearchPreparationTextSamples = vi.hoisted(() => vi.fn());
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
  importFileLogicalPath: (file: File) => (
    (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
  ),
}));

vi.mock('../../src/api/searchPreparation', () => ({
  getSearchPreparation, getSearchPreparationTextSamples,
  createSearchPreparation: vi.fn(), decideSearchPreparation: vi.fn(), stopSearchPreparation: vi.fn(),
}));

import { installDialogPolyfill } from '../support/domPolyfills';
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
  installDialogPolyfill();
  getSearchPreparation.mockResolvedValue({
    schemaVersion: 'frisket.search_preparation.v1', id: 'prep-1', sheet_id: 9,
    plan_hash: 'sha256:plan', status: 'complete',
    groups: [{ kind: 'native_pdf', count: 1, status: 'complete' }],
    models: [], jobs: [], can_stop: false, can_setup: false, errors: [],
  });
  getSearchPreparationTextSamples.mockResolvedValue({
    batch_id: 'prep-1', samples: [{
      row_id: 42, column_id: 5, filename: 'native.pdf', page_number: 1,
      page_count: 1, page_scoped: true, preview_url: '/preview',
      text: 'Automatically previewed PDF text', source: 'prepared', truncated: false,
    }],
  });
  listImportSessions.mockResolvedValue({ sessions: [running] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe('persistent import progress', () => {
  it('opens preparation in a dialog and returns to the compact import banner when done', async () => {
    listImportSessions.mockResolvedValue({ sessions: [{
      ...running, state: 'completed', committed_rows: 10, search_preparation_batch_id: 'prep-1',
    }] });
    const onOpenSheet = vi.fn();
    render(<ImportProgress projectId="project-1" onOpenSheet={onOpenSheet} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Open preparation' }));

    const dialog = await screen.findByRole('dialog', { name: 'Search preparation' });
    expect(within(dialog).getByRole('button', { name: 'Close preparation' })).toHaveFocus();
    expect(await within(dialog).findByText('Automatically previewed PDF text')).toBeVisible();
    const banner = screen.getByRole('region', { name: 'Import progress' });
    expect(within(banner).queryByTestId('search-preparation-receipt')).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Done' }));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(banner).toHaveTextContent('10 rows added');
    fireEvent.click(within(banner).getByRole('button', { name: 'Open sheet' }));
    expect(onOpenSheet).toHaveBeenCalledWith(9);
  });

  it('allows closing preparation without making a preparation decision', async () => {
    listImportSessions.mockResolvedValue({ sessions: [{
      ...running, search_preparation_batch_id: 'prep-1',
    }] });
    render(<ImportProgress projectId="project-1" onOpenSheet={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Open preparation' }));
    const dialog = await screen.findByRole('dialog', { name: 'Search preparation' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close preparation' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Open preparation' }));
    fireEvent(await screen.findByRole('dialog', { name: 'Search preparation' }), new Event('cancel', { cancelable: true }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Open preparation' })).toBeVisible();
  });

  it('closes preparation before a changed project can read or poll the old batch', async () => {
    vi.useFakeTimers();
    listImportSessions.mockResolvedValue({ sessions: [{
      ...running, state: 'completed', search_preparation_batch_id: 'prep-1',
    }] });
    getSearchPreparation.mockResolvedValue({
      schemaVersion: 'frisket.search_preparation.v1', id: 'prep-1', sheet_id: 9,
      plan_hash: 'sha256:plan', status: 'preparing', groups: [], models: [],
      jobs: [], can_stop: true, can_setup: false, errors: [],
    });
    const onOpenSheet = vi.fn();
    const { rerender } = render(<ImportProgress projectId="project-1" onOpenSheet={onOpenSheet} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    fireEvent.click(screen.getByRole('button', { name: 'Open preparation' }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole('dialog', { name: 'Search preparation' })).toBeVisible();
    expect(getSearchPreparation).toHaveBeenCalledTimes(1);

    rerender(<ImportProgress projectId="project-2" onOpenSheet={onOpenSheet} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(getSearchPreparation).toHaveBeenCalledTimes(1);
    expect(getSearchPreparation).toHaveBeenCalledWith('project-1', 'prep-1');

    rerender(<ImportProgress projectId="project-1" onOpenSheet={onOpenSheet} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

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
    await rememberImportSessionFiles('import-1', [file]);
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
    await rememberImportSessionFiles('import-1', [file]);
    render(<ImportProgress projectId="project-1" onOpenSheet={vi.fn()} />);

    await screen.findByText(/Upload was interrupted/);
    fireEvent.click(screen.getByRole('button', { name: 'Reselect files' }));
    fireEvent.change(screen.getByLabelText('Reselect import files'), { target: { files: [file] } });
    await waitFor(() => expect(uploadFilesToImportSession).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(cancelImportSession).toHaveBeenCalledWith('project-1', 'import-1'));
    await act(async () => { finishUpload(); });
  });

  it('does not abort one session preparation when another session is cancelled', async () => {
    const preparing = {
      ...running, import_ref: 'import-a', state: 'admitting', through: 0,
      committed_rows: 0, sheet_id: null, sealed: false,
    };
    const other = { ...running, import_ref: 'import-b' };
    listImportSessions.mockResolvedValue({ sessions: [preparing, other] });
    cancelImportSession.mockResolvedValue({ ...other, state: 'cancelling', cancel_requested: true });
    uploadFilesToImportSession.mockResolvedValue({ ...preparing, state: 'running', sealed: true });
    const files = Array.from({ length: 2_048 }, (_, index) => new File(['x'], `${index}.pdf`));
    await rememberImportSessionFiles('import-a', files);
    render(<ImportProgress projectId="project-1" onOpenSheet={vi.fn()} />);

    const interrupted = await screen.findByText(/Upload was interrupted/);
    fireEvent.click(within(interrupted.parentElement!).getByRole('button', { name: 'Reselect files' }));
    fireEvent.change(screen.getByLabelText('Reselect import files'), { target: { files } });
    expect(screen.getByText(/Preparing files/)).toBeInTheDocument();
    const otherSession = screen.getAllByTestId('import-progress-running')
      .find((element) => element.textContent?.includes('3 of 10'))!;
    fireEvent.click(within(otherSession).getByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(cancelImportSession).toHaveBeenCalledWith('project-1', 'import-b'));
    await waitFor(() => expect(uploadFilesToImportSession).toHaveBeenCalledWith(
      'project-1', expect.objectContaining({ import_ref: 'import-a' }), files,
    ));
  });
});
