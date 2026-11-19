// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { ProjectApi } from '../../src/api/ports';
import type { ProvenanceManifest as ProvenanceManifestData } from '../../src/api/open';
import { ProvenanceManifest } from '../../src/components/ProvenanceManifest';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

// The panel reads three members of the project API; the rest is not built.
function fakeProjectApi(members: Record<string, unknown>): ProjectApi {
  return members as unknown as ProjectApi;
}

beforeEach(() => { vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(new Date('2026-11-17T12:00:00Z')); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

function manifest(runsOffset: number, receiptsOffset: number): ProvenanceManifestData {
  const page = (offset: number, schemaVersion: 'frisket.provenance_runs_page.v1' | 'frisket.provenance_receipts_page.v1') => ({
    schemaVersion, order: 'desc' as const, offset, limit: 25, total: 60,
    hasMore: offset + 25 < 60, nextOffset: offset + 25 < 60 ? offset + 25 : null,
  });
  return {
    projectId: 'paging', models: [], touched: [], providers: [], totalCost: 0, hasUnknownCosts: false,
    unknownCostRuns: 0, actionKinds: [], runs: [], receipts: [],
    runsPage: page(runsOffset, 'frisket.provenance_runs_page.v1'),
    receiptsPage: page(receiptsOffset, 'frisket.provenance_receipts_page.v1'),
  };
}

it('pins mount, paging, refresh and stale-completion markup', async () => {
  const requests: Array<ReturnType<typeof deferred<ProvenanceManifestData>>> = [];
  const getProvenanceManifest = vi.fn(() => {
    const request = deferred<ProvenanceManifestData>();
    requests.push(request);
    return request.promise;
  });
  const projectApi = fakeProjectApi({
    getProvenanceManifest,
    qa: { citation: vi.fn(), report: vi.fn() },
    listAttemptReceipts: vi.fn(() => new Promise(() => {})),
  });
  const harness = createWorkspaceTestHarness({ projectId: 'paging', api: { projectApi } });
  const { container, unmount } = harness.render(<ProvenanceManifest projectName="Paging" onClose={vi.fn()} />);
  const steps = [container.innerHTML];
  expect(getProvenanceManifest).toHaveBeenLastCalledWith(0, 25, 0, 25);

  fireEvent.click(screen.getByTestId('provenance-refresh'));
  expect(getProvenanceManifest).toHaveBeenLastCalledWith(0, 25, 0, 25);
  requests[0].resolve(manifest(50, 50)); await flush(); steps.push(container.innerHTML);
  requests[1].resolve(manifest(0, 0)); await flush(); steps.push(container.innerHTML);

  fireEvent.click(screen.getByTestId('provenance-runs-older'));
  expect(getProvenanceManifest).toHaveBeenLastCalledWith(25, 25, 0, 25);
  steps.push(container.innerHTML);
  requests[2].resolve(manifest(25, 0)); await flush(); steps.push(container.innerHTML);

  fireEvent.click(screen.getByTestId('provenance-refresh'));
  expect(getProvenanceManifest).toHaveBeenLastCalledWith(25, 25, 0, 25);
  requests[3].reject(new Error('Provenance unavailable')); await flush(); steps.push(container.innerHTML);

  fireEvent.click(screen.getByTestId('provenance-refresh'));
  expect(getProvenanceManifest).toHaveBeenLastCalledWith(25, 25, 0, 25);
  unmount();
  requests[4].resolve(manifest(0, 0)); await flush();
  expect(getProvenanceManifest).toHaveBeenCalledTimes(5);
  expect(steps).toMatchSnapshot();
});

it('ignores a mount completion that lands after unmount', async () => {
  const request = deferred<ProvenanceManifestData>();
  const projectApi = fakeProjectApi({
    getProvenanceManifest: vi.fn(() => request.promise),
    qa: { citation: vi.fn(), report: vi.fn() },
    listAttemptReceipts: vi.fn(() => new Promise(() => {})),
  });
  const errors = vi.spyOn(console, 'error');
  const harness = createWorkspaceTestHarness({ projectId: 'paging-unmount', api: { projectApi } });
  const { unmount } = harness.render(<ProvenanceManifest projectName="Paging" onClose={vi.fn()} />);
  unmount();
  request.resolve(manifest(0, 0)); await flush();
  expect(errors).not.toHaveBeenCalled();
});

it('announces a manifest load failure as an alert', async () => {
  const projectApi = fakeProjectApi({
    getProvenanceManifest: vi.fn(() => Promise.reject(new Error('Provenance unavailable'))),
    qa: { citation: vi.fn(), report: vi.fn() },
    listAttemptReceipts: vi.fn(() => new Promise(() => {})),
  });
  const harness = createWorkspaceTestHarness({ projectId: 'paging-error', api: { projectApi } });
  harness.render(<ProvenanceManifest projectName="Paging" onClose={vi.fn()} />);
  await flush();
  expect(screen.getByRole('alert')).toBe(screen.getByTestId('provenance-error'));
});
