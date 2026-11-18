// @vitest-environment jsdom
import { useEffect } from 'react';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import type { ProjectInfo } from '../../src/api/open';
import { WorkspaceStoresProvider } from '../../src/bind/WorkspaceStoresProvider';
import { openRunReview } from '../../src/components/review/openRunReview';
import { useWorkspaceModel } from '../../src/workspace/useWorkspaceModel';

const PROJECT: ProjectInfo = { id: 'p1', name: 'Open run review' };

let latest: ReturnType<typeof useWorkspaceModel> | null = null;

function Probe() {
  const current = useWorkspaceModel({ project: PROJECT });
  useEffect(() => {
    latest = current;
  });
  return null;
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', {
    status: 200, headers: { 'content-type': 'application/json' },
  })));
});

afterEach(() => {
  cleanup();
  latest = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it('opens Review on the requested run when asked to open a run', async () => {
  render(<WorkspaceStoresProvider projectId={PROJECT.id}><Probe /></WorkspaceStoresProvider>);
  await waitFor(() => expect(latest).not.toBeNull());
  expect(latest?.overlayModel.reviewOpen).toBe(false);
  act(() => openRunReview('12'));
  await waitFor(() => expect(latest?.overlayModel.reviewOpen).toBe(true));
  expect(latest?.overlayModel.reviewRunId).toBe('12');
});
