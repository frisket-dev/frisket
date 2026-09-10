// @vitest-environment jsdom
//
// Ruling 4 ("a retry is a resume"): a run the recipe HALTED must keep its way
// back at the surface that reports the halt. Before this, the Monitor dock's
// job detail said "Stopped because …" and threw the resume metadata away,
// leaving the user to hunt for the column drawer.
//
// Pins the three honest shapes of the affordance:
//   * a resumable halt (resume_action run.backfill, i.e. any halted_code other
//     than promise_violation) with a known target column offers ONE resume
//     button that routes the job to the host's resume callback — the same
//     cost-gated run.backfill door every compliant retry surface uses;
//   * a promise_violation halt resumes through the same run.backfill door,
//     but that door re-asks for consent to the changed terms and this panel
//     cannot render a confirmation — it gets the honest next step and NO
//     dead button;
//   * a halt whose launch context is gone (no target column, e.g. after a
//     reload) points at the column drawer's own resume door instead of
//     offering a button it cannot honor.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      // The detail pane's Charges section (AttemptReceiptList) loads per run;
      // an empty page renders its honest "no charges" state.
      listAttemptReceipts: vi.fn().mockResolvedValue({
        attempts: [],
        total: 0,
        has_more: false,
      }),
    },
  };
});

import { WorkbenchJobSplitPanel, type DockActionJob } from '../../src/workbench/WorkbenchBottomDock';
import type { RunProgress } from '../../src/api/open';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const projectApi = createProjectApi('p1');
vi.spyOn(projectApi, 'listAttemptReceipts').mockResolvedValue({
  attempts: [], total: 0, has_more: false,
});
const { render } = createWorkspaceTestHarness({
  projectId: 'p1',
  api: { projectApi: projectApi },
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function haltedProgress(overrides: Partial<RunProgress> = {}): RunProgress {
  return {
    runId: '7',
    actionName: 'Transcribe audio',
    actionKind: 'media.transcribe',
    sheetId: '3',
    targetColumnId: 'transcript',
    targetRowIds: null,
    status: 'cancelled',
    completedRows: 12,
    totalRows: 40,
    failedRows: 0,
    costSoFar: 1.25,
    haltedCode: 'local_artifact_unavailable',
    haltedReason: 'The local model is not downloaded yet.',
    ...overrides,
  };
}

function haltedJob(progress: RunProgress | undefined): DockActionJob {
  return {
    schemaVersion: 'frisket.job.v1',
    projectId: 'p1',
    jobId: 42,
    kind: 'action',
    runId: progress?.runId ?? '7',
    receiptId: null,
    status: 'cancelled',
    actionKind: 'media.transcribe',
    actionName: 'Transcribe audio',
    attempts: 1,
    maxAttempts: 3,
    lease: { lockedBy: null, lockedAt: null, leaseExpiresAt: null, leaseExpired: false },
    timing: { createdAt: null, startedAt: null, finishedAt: null },
    error: null,
    ...(progress ? { progress } : {}),
  };
}

function renderPanel(job: DockActionJob, onResumeHaltedRun?: (job: DockActionJob) => void) {
  const liveActionJobs = { start: vi.fn(), dispose: vi.fn() };
  return render(
    <WorkbenchJobSplitPanel
      jobs={[job]}
      loading={false}
      error={null}
      liveActionJobs={liveActionJobs}
      mode="jobs"
      onResumeHaltedRun={onResumeHaltedRun}
    />,
  );
}

describe('Monitor dock — halted-run resume (ruling 4)', () => {
  it('a zero-row halt tells the user to start again and never offers resume', async () => {
    const resumed: DockActionJob[] = [];
    const job = haltedJob(
      haltedProgress({
        completedRows: 0,
        haltedCode: 'promise_violation',
        haltedReason: 'The consented promise set changed before dispatch.',
      }),
    );
    renderPanel(job, (resumedJob) => resumed.push(resumedJob));

    const note = await screen.findByTestId('bottom-dock-halt-restart');
    expect(note).toHaveTextContent(
      'Nothing was published. Start Transcribe audio again from Actions.',
    );
    expect(screen.queryByTestId('bottom-dock-halt-resume-run')).not.toBeInTheDocument();
    expect(screen.queryByTestId('bottom-dock-halt-reconfirm')).not.toBeInTheDocument();
    expect(screen.queryByTestId('bottom-dock-halt-resume-hint')).not.toBeInTheDocument();
    expect(resumed).toHaveLength(0);
  });

  it('offers a resume that hands the halted job to the run.backfill door, once', async () => {
    const resumed: DockActionJob[] = [];
    const job = haltedJob(haltedProgress());
    renderPanel(job, (resumedJob) => resumed.push(resumedJob));

    // The halt keeps its reason…
    expect(await screen.findByText('Stopped because')).toBeInTheDocument();
    expect(screen.getByText('The local model is not downloaded yet.')).toBeInTheDocument();

    // …and the resume says what is kept before the click.
    const section = screen.getByTestId('bottom-dock-halt-resume');
    expect(section).toHaveTextContent('12 of 40 rows finished before the stop');

    const button = screen.getByTestId('bottom-dock-halt-resume-run');
    expect(button).toHaveTextContent('Resume run');
    fireEvent.click(button);

    // The job (carrying its own sheet + target column) reached the host's
    // resume door exactly once, and the button says so instead of re-arming.
    expect(resumed).toHaveLength(1);
    expect(resumed[0].progress?.sheetId).toBe('3');
    expect(resumed[0].progress?.targetColumnId).toBe('transcript');
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent('Resume requested');
    fireEvent.click(button);
    expect(resumed).toHaveLength(1);
  });

  it('a promise_violation halt explains the re-confirm and offers NO dead button', async () => {
    const resumed: DockActionJob[] = [];
    const job = haltedJob(
      haltedProgress({
        haltedCode: 'promise_violation',
        haltedReason: 'The consented promise set changed.',
      }),
    );
    renderPanel(job, (resumedJob) => resumed.push(resumedJob));

    const note = await screen.findByTestId('bottom-dock-halt-reconfirm');
    expect(note).toHaveTextContent('resuming needs your consent to the new ones');
    expect(note).toHaveTextContent('rows that already finished are kept');
    // No one-click button: this halt must re-confirm the changed terms first.
    expect(screen.queryByTestId('bottom-dock-halt-resume-run')).not.toBeInTheDocument();
    expect(resumed).toHaveLength(0);
  });

  it('a halt without launch context points at the column drawer instead of a button', async () => {
    const job = haltedJob(haltedProgress({ targetColumnId: '' }));
    renderPanel(job, () => {});

    const hint = await screen.findByTestId('bottom-dock-halt-resume-hint');
    expect(hint).toHaveTextContent('Run missing cells');
    expect(screen.queryByTestId('bottom-dock-halt-resume-run')).not.toBeInTheDocument();
  });

  it('a job that was not halted gets no resume section at all', async () => {
    const job = haltedJob(
      haltedProgress({ status: 'complete', haltedCode: null, haltedReason: null }),
    );
    renderPanel(job, () => {});

    await screen.findByTestId('bottom-dock-job-42');
    expect(screen.queryByTestId('bottom-dock-halt-resume')).not.toBeInTheDocument();
    expect(screen.queryByTestId('bottom-dock-halt-resume-hint')).not.toBeInTheDocument();
    expect(screen.queryByTestId('bottom-dock-halt-reconfirm')).not.toBeInTheDocument();
  });
});
