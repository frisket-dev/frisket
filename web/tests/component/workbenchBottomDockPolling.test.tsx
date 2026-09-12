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
            liveActionJobs={liveActionJobs}
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
    />,
  );
}

beforeEach(() => localStorage.clear());

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('WorkbenchBottomDock — live Jobs/Errors resource interest', () => {
  it('activates only while Jobs or Errors is mounted and expanded', async () => {
    const liveActionJobs = { start: vi.fn(), dispose: vi.fn() };
    const view = renderDock('jobs', liveActionJobs);
    await waitFor(() => expect(liveActionJobs.start).toHaveBeenCalledTimes(1));

    view.rerender(
      <WorkbenchBottomDock
        region={region}
        activeTabPlacementId="history"
        onSelectTab={() => undefined}
        onCloseTab={() => undefined}
        renderContribution={(active) => active.contributionId === 'frisket.core.panel.history'
          ? <div>{active.title}</div>
          : (
              <WorkbenchJobSplitPanel
                jobs={[]}
                loading={false}
                error={null}
                liveActionJobs={liveActionJobs}
                mode={active.contributionId === 'frisket.core.panel.errors' ? 'errors' : 'jobs'}
              />
            )}
      />,
    );
    await waitFor(() => expect(liveActionJobs.dispose).toHaveBeenCalledTimes(1));

    view.rerender(
      <WorkbenchBottomDock
        region={region}
        activeTabPlacementId="errors"
        onSelectTab={() => undefined}
        onCloseTab={() => undefined}
        renderContribution={(active) => active.contributionId === 'frisket.core.panel.history'
          ? <div>{active.title}</div>
          : (
              <WorkbenchJobSplitPanel
                jobs={[]}
                loading={false}
                error={null}
                liveActionJobs={liveActionJobs}
                mode={active.contributionId === 'frisket.core.panel.errors' ? 'errors' : 'jobs'}
              />
            )}
      />,
    );
    await waitFor(() => expect(liveActionJobs.start).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole('button', { name: 'Minimize the Monitor dock' }));
    await waitFor(() => expect(liveActionJobs.dispose).toHaveBeenCalledTimes(2));
  });

  it('reuses one lane across Jobs to Errors without cleanup/start flapping', async () => {
    const liveActionJobs = { start: vi.fn(), dispose: vi.fn() };
    const view = renderDock('jobs', liveActionJobs);
    await waitFor(() => expect(liveActionJobs.start).toHaveBeenCalledTimes(1));

    view.rerender(
      <WorkbenchBottomDock
        region={region}
        activeTabPlacementId="errors"
        onSelectTab={() => undefined}
        onCloseTab={() => undefined}
        renderContribution={(active) => (
          <WorkbenchJobSplitPanel
            jobs={[]}
            loading={false}
            error={null}
            liveActionJobs={liveActionJobs}
            mode={active.contributionId === 'frisket.core.panel.errors' ? 'errors' : 'jobs'}
          />
        )}
      />,
    );

    expect(liveActionJobs.start).toHaveBeenCalledTimes(1);
    expect(liveActionJobs.dispose).not.toHaveBeenCalled();
    view.unmount();
    expect(liveActionJobs.dispose).toHaveBeenCalledTimes(1);
  });

  it('does not activate for a selected contribution whose panel is not rendered', () => {
    const liveActionJobs = { start: vi.fn(), dispose: vi.fn() };
    render(
      <WorkbenchBottomDock
        region={region}
        activeTabPlacementId="jobs"
        onSelectTab={() => undefined}
        onCloseTab={() => undefined}
        renderContribution={() => null}
      />,
    );
    expect(liveActionJobs.start).not.toHaveBeenCalled();
    expect(liveActionJobs.dispose).not.toHaveBeenCalled();
  });
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
      liveActionJobs={{ start: vi.fn(), dispose: vi.fn() }}
      mode="jobs"
    />,
  );

  expect(screen.getByText('Preparing')).toBeTruthy();
  expect(screen.getByText('Preparing the language model if this worker needs it, then translating…')).toBeTruthy();
});
