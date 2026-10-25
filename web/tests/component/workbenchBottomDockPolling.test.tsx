// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';


import {
  WorkbenchBottomDock,
  WorkbenchJobSplitPanel,
} from '../../src/workbench/WorkbenchBottomDock';
import type { DockActionJob } from '../../src/workbench/WorkbenchBottomDock';
import type {
  WorkbenchResolvedLayoutContribution,
  WorkbenchResolvedLayoutRegion,
} from '../../src/workbench/layout';

function contribution(
  contributionId: string,
  placementId: string,
  title: string,
): WorkbenchResolvedLayoutContribution {
  return {
    contributionId,
    title,
    ownerPluginId: 'frisket.core',
    host: 'bottomDock',
    mode: 'tab',
    slot: 'bottomDock',
    placementId,
    status: 'enabled',
    runtimeSource: 'firstParty',
  };
}

const jobs = contribution('frisket.core.panel.jobs', 'jobs', 'Jobs');
const errors = contribution('frisket.core.panel.errors', 'errors', 'Errors');
const history = contribution('frisket.core.panel.history', 'history', 'History');
const region: WorkbenchResolvedLayoutRegion = {
  regionId: 'bottomDock',
  contributions: [jobs, errors, history],
};

function renderDock(activeTabPlacementId: string, liveActionJobs: {
  start(): void;
  dispose(): void;
}) {
  const renderContribution = (active: WorkbenchResolvedLayoutContribution) => (
    active.contributionId === 'frisket.core.panel.jobs'
    || active.contributionId === 'frisket.core.panel.errors'
      ? (
          <WorkbenchJobSplitPanel
            jobs={[]}
            loading={false}
            error={null}
            mode={active.contributionId === 'frisket.core.panel.errors' ? 'errors' : 'jobs'}
          />
        )
      : <div>{active.title}</div>
  );
  return render(
    <WorkbenchBottomDock
      region={region}
      activeTabPlacementId={activeTabPlacementId}
      onSelectTab={() => undefined}
      onCloseTab={() => undefined}
      renderContribution={renderContribution}
      liveActionJobs={liveActionJobs}
    />,
  );
}

beforeEach(() => localStorage.clear());

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('WorkbenchBottomDock — live Jobs/Errors resource interest', () => {
  it('keeps the shared job subscription while another tab is selected or the dock is collapsed', async () => {
    const liveActionJobs = { start: vi.fn(), dispose: vi.fn() };
    const view = renderDock('jobs', liveActionJobs);
    await waitFor(() => expect(liveActionJobs.start).toHaveBeenCalledTimes(1));

    view.rerender(
      <WorkbenchBottomDock
        region={region}
        activeTabPlacementId="history"
        onSelectTab={() => undefined}
        onCloseTab={() => undefined}
        liveActionJobs={liveActionJobs}
        renderContribution={(active) => active.contributionId === 'frisket.core.panel.history'
          ? <div>{active.title}</div>
          : (
              <WorkbenchJobSplitPanel
                jobs={[]}
                loading={false}
                error={null}
                mode={active.contributionId === 'frisket.core.panel.errors' ? 'errors' : 'jobs'}
              />
            )}
      />,
    );
    expect(liveActionJobs.start).toHaveBeenCalledTimes(1);
    expect(liveActionJobs.dispose).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Minimize the Monitor dock' }));
    expect(liveActionJobs.start).toHaveBeenCalledTimes(1);
    expect(liveActionJobs.dispose).not.toHaveBeenCalled();
    view.unmount();
    expect(liveActionJobs.dispose).toHaveBeenCalledTimes(1);
  });

  it('releases the previous project subscription before starting its replacement', async () => {
    const first = { start: vi.fn(), dispose: vi.fn() };
    const second = { start: vi.fn(), dispose: vi.fn() };
    const view = renderDock('jobs', first);
    await waitFor(() => expect(first.start).toHaveBeenCalledTimes(1));

    view.rerender(
      <WorkbenchBottomDock
        region={region}
        activeTabPlacementId="jobs"
        onSelectTab={() => undefined}
        onCloseTab={() => undefined}
        liveActionJobs={second}
        renderContribution={(active) => (
          <WorkbenchJobSplitPanel
            jobs={[]}
            loading={false}
            error={null}
            mode={active.contributionId === 'frisket.core.panel.errors' ? 'errors' : 'jobs'}
          />
        )}
      />,
    );

    await waitFor(() => expect(second.start).toHaveBeenCalledTimes(1));
    expect(first.dispose).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(second.dispose).toHaveBeenCalledTimes(1);
  });
});

it('labels an all-skipped terminal summary as partial, not all complete', () => {
  const liveActionJobs = { start: vi.fn(), dispose: vi.fn() };
  render(
    <WorkbenchBottomDock
      region={region}
      activeTabPlacementId="jobs"
      onSelectTab={() => undefined}
      onCloseTab={() => undefined}
      renderContribution={() => <div>Jobs</div>}
      liveActionJobs={liveActionJobs}
      runSummary={{
        runningCount: 0,
        anyComplete: true,
        anyPartial: true,
        anyErrored: false,
        percent: null,
      }}
    />,
  );

  const summary = screen.getByTestId('bottom-dock-run-summary');
  expect(summary).toHaveAttribute('data-run-state', 'partial');
  expect(summary).toHaveTextContent('partial');
  expect(summary).not.toHaveTextContent('all complete');
});

it('shows the preparation hint in a zero-row running job detail', () => {
  const job: DockActionJob = {
    schemaVersion: 'frisket.action_job.v1', projectId: 'project-a', jobId: 1,
    kind: 'action', runId: null, receiptId: null, status: 'running',
    actionKind: 'map.translate', actionName: 'Translate', attempts: 1, maxAttempts: 3,
    lease: { lockedBy: null, lockedAt: null, leaseExpiresAt: null, leaseExpired: false },
    timing: { createdAt: '2026-09-12T00:00:00Z', startedAt: '2026-09-12T00:00:01Z', finishedAt: null },
    error: null,
    progress: {
      runId: 'run-1', actionName: 'Translate', actionKind: 'map.translate', sheetId: '1',
      targetColumnId: 'result', status: 'running', completedRows: 0, totalRows: 10,
      failedRows: 0, costSoFar: 0,
      preparation: {
        message: 'Preparing the language model if this worker needs it, then translating…',
      },
    },
  };
  render(
    <WorkbenchJobSplitPanel
      jobs={[job]}
      loading={false}
      error={null}
      mode="jobs"
    />,
  );

  expect(screen.getByText('Preparing')).toBeTruthy();
  expect(screen.getByText('Preparing the language model if this worker needs it, then translating…')).toBeTruthy();
});

it('shows authoritative public outcome counters in job detail', () => {
  const job: DockActionJob = {
    schemaVersion: 'frisket.action_job.v1', projectId: 'project-a', jobId: 2,
    kind: 'action', runId: null, receiptId: null, status: 'done',
    actionKind: 'map.clean_dates', actionName: 'Clean dates', attempts: 1, maxAttempts: 3,
    lease: { lockedBy: null, lockedAt: null, leaseExpiresAt: null, leaseExpired: false },
    timing: { createdAt: '2026-10-25T00:00:00Z', startedAt: '2026-10-25T00:00:01Z', finishedAt: '2026-10-25T00:00:02Z' },
    error: null,
    progress: {
      runId: 'run-2', actionName: 'Clean dates', actionKind: 'map.clean_dates', sheetId: '1',
      targetColumnId: 'cleaned', status: 'partial', completedRows: 3, totalRows: 5,
      failedRows: 1, attemptedRows: 3, succeededRows: 2, skippedRows: 2,
      remainingRows: 0, costSoFar: 0,
    },
  };
  render(
    <WorkbenchJobSplitPanel
      jobs={[job]}
      loading={false}
      error={null}
      mode="jobs"
    />,
  );

  expect(screen.getAllByText('partial')).not.toHaveLength(0);
  expect(screen.getByText('Attempted rows')).toBeTruthy();
  expect(screen.getByText('Succeeded rows')).toBeTruthy();
  expect(screen.getByText('Skipped rows')).toBeTruthy();
  expect(screen.getByText('Remaining rows')).toBeTruthy();
  expect(screen.getByText('Succeeded rows').parentElement).toHaveTextContent('2');
  expect(screen.getByText('Skipped rows').parentElement).toHaveTextContent('2');
});
