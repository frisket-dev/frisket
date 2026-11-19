// @vitest-environment jsdom
import { useEffect, useLayoutEffect, type ReactNode } from 'react';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import type { HistoryState, ProjectInfo } from '../../src/api/open';
import type { ReviewRun, ReviewRunsPage } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { openRunLink, runLinkHref } from '../../src/routes';
import { createWorkspaceStores } from '../../src/state/createWorkspaceStores';
import { useWorkspaceModel } from '../../src/workspace/useWorkspaceModel';

const REVIEW_RUN: ReviewRun = {
  runId: '12', sourceId: '12', copiedChangeId: null, sheetId: '3', sheetName: 'Articles', actionKind: 'map.classify',
  actionName: 'Classify', model: 'model-1', startedAt: '2026-09-01T12:00:00Z',
  reviewStatus: 'open', reviewCompletedAt: null,
  total: {
    eligibleCount: 1, reviewedCount: 0, acceptedCount: 0, incorrectCount: 0, unreviewedCount: 1, confidenceCount: 0,
  },
  fields: [],
};

function reviewPage(runs: ReviewRun[]): ReviewRunsPage {
  return {
    schemaVersion: 'frisket.review_runs_page.v1', runs,
    offset: 0, limit: 1, total: runs.length, hasMore: false, nextOffset: null,
  };
}

const HISTORY: HistoryState = {
  changes: [
    { id: '40', index: 0, label: 'Imported Articles', kind: 'import', at: '2026-11-15T00:00:00Z', rowsAffected: 2, status: 'applied', executable: true },
    {
      id: '41', index: 1, label: 'Extract names', kind: 'map', at: '2026-11-15T00:01:00Z', rowsAffected: 2, status: 'applied', executable: true,
      run: {
        runId: '13', status: 'complete', actionKind: 'map.ner', model: null, params: {}, totalRows: 2, completedRows: 2,
        failedRows: 0, costActual: null, startedAt: '2026-11-15T00:01:00Z', finishedAt: '2026-11-15T00:01:05Z',
        workerVersion: null, generatedColumns: [],
      },
    },
  ],
  cursorIndex: 1, cursorChange: null, cursorChangeLoaded: true, undoTarget: null, redoTarget: null,
  offset: 0, limit: 50, total: 2, hasMoreBefore: false, hasMoreAfter: false, prevOffset: null, nextOffset: null,
  revision: { total: 2, maxChangeId: '41', changeCursor: '41' },
};

let latest: ReturnType<typeof useWorkspaceModel> | null = null;

function Probe({ project }: { project: ProjectInfo }) {
  const current = useWorkspaceModel({ project });
  useEffect(() => {
    latest = current;
  });
  return null;
}

function renderProject(projectId: string) {
  const projectApi = createProjectApi(projectId);
  const listReviewRuns = vi.spyOn(projectApi, 'listReviewRuns').mockImplementation(
    async (_offset, _limit, options) => reviewPage(options?.runId === '12' ? [REVIEW_RUN] : []),
  );
  vi.spyOn(projectApi, 'getHistory').mockResolvedValue(HISTORY);
  const stores = createWorkspaceStores(projectId, projectApi);
  const setError = vi.spyOn(stores.chrome, 'setError');
  function Session({ children }: { children: ReactNode }) {
    useLayoutEffect(() => {
      stores.lease.activate();
      return () => stores.lease.deactivate();
    }, []);
    return <WorkspaceStoresContext.Provider value={stores}>{children}</WorkspaceStoresContext.Provider>;
  }
  const view = render(<Session><Probe project={{ id: projectId, name: projectId }} /></Session>);
  return { ...view, stores, listReviewRuns, setError };
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', {
    status: 200, headers: { 'content-type': 'application/json' },
  })));
});

afterEach(() => {
  cleanup();
  latest = null;
  window.history.replaceState(null, '', '/');
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it('opens a run with results in Review and consumes the parameter', async () => {
  window.history.replaceState(null, '', runLinkHref('p1', undefined, '12'));
  renderProject('p1');
  await waitFor(() => expect(latest?.overlayModel.reviewOpen).toBe(true));
  expect(latest?.overlayModel.reviewRunId).toBe('12');
  expect(window.location.search).toBe('');
});

it('opens a run with nothing to review on its History change', async () => {
  window.history.replaceState(null, '', runLinkHref('p1', undefined, '13'));
  const { stores } = renderProject('p1');
  await waitFor(() => expect(stores.chrome.store.get().historyFocusChangeId).toBe('41'));
  expect(stores.chrome.store.get().activeBottomDockTab).toBe('history');
  expect(latest?.overlayModel.reviewOpen).toBe(false);
  expect(window.location.search).toBe('');
});

it('leaves an unknown run on the project view with a notice', async () => {
  window.history.replaceState(null, '', runLinkHref('p1', undefined, '99'));
  const { stores, setError } = renderProject('p1');
  await waitFor(() => expect(setError).toHaveBeenCalledWith({ message: 'Run #99 was not found in this project.' }));
  expect(latest?.overlayModel.reviewOpen).toBe(false);
  expect(stores.chrome.store.get().historyFocusChangeId).toBeNull();
  expect(window.location.search).toBe('');
});

it('does not reopen a consumed run on reload', async () => {
  window.history.replaceState(null, '', runLinkHref('p1', undefined, '12'));
  const first = renderProject('p1');
  await waitFor(() => expect(latest?.overlayModel.reviewOpen).toBe(true));
  cleanup();
  window.history.replaceState(null, '', '/p/p1');
  const reloaded = renderProject('p1');
  await waitFor(() => expect(latest).not.toBeNull());
  expect(first.listReviewRuns).toHaveBeenCalledTimes(1);
  expect(reloaded.listReviewRuns).not.toHaveBeenCalled();
});

it('opens the run in the project the link switched to', async () => {
  window.history.replaceState(null, '', '/p/p1');
  const previous = renderProject('p1');
  await waitFor(() => expect(latest).not.toBeNull());
  act(() => openRunLink('p2', '12'));
  expect(window.location.pathname).toBe('/p/p2');
  // The project being left never takes another project's run.
  expect(previous.listReviewRuns).not.toHaveBeenCalled();
  expect(new URLSearchParams(window.location.search).get('run')).toBe('12');
  cleanup();
  renderProject('p2');
  await waitFor(() => expect(latest?.overlayModel.reviewOpen).toBe(true));
  expect(latest?.overlayModel.reviewRunId).toBe('12');
  expect(window.location.search).toBe('');
});

it('opens a run link to the open project without a reload', async () => {
  window.history.replaceState(null, '', '/p/p1');
  renderProject('p1');
  await waitFor(() => expect(latest).not.toBeNull());
  act(() => openRunLink('p1', '12'));
  await waitFor(() => expect(latest?.overlayModel.reviewRunId).toBe('12'));
  expect(window.location.search).toBe('');
});
