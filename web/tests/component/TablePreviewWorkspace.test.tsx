// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useEffect } from 'react';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { WorkspaceStoresProvider } from '../../src/bind/WorkspaceStoresProvider';
import { useWorkspaceModel } from '../../src/workspace/useWorkspaceModel';
import { ActionPreviewBanner } from '../../src/components/ActionPreviewBanner';
import type { RegisteredActionRequest } from '../../src/api/types';

const PROJECT = { id: 'empty-project', name: 'Empty project' };
let latest: ReturnType<typeof useWorkspaceModel> | null = null;
function Probe() {
  const model = useWorkspaceModel({ project: PROJECT });
  useEffect(() => { latest = model; });
  const preview = model.mainViewModel.activePreviewView;
  return <>{preview && <ActionPreviewBanner view={preview}
    onRun={model.mainViewModel.runPreviewForReal} onClose={model.mainViewModel.closePreviewView} />}
    {model.mainViewModel.gridContribution}</>;
}
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { 'Content-Type': 'application/json' },
});
afterEach(() => { cleanup(); latest = null; vi.unstubAllGlobals(); });

it('renders an ordered table in an empty workspace and Run for real uses normal admission with a fresh key', async () => {
  const request: RegisteredActionRequest = { action_id: 'import.files', scope: { kind: 'project' },
    params: { path: '/fixture' }, output_names: {}, sheet_name: 'Files', idempotency_key: 'preview-intent' };
  const previews: unknown[] = [];
  const runs: Record<string, unknown>[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/actions/v1/preview') && init?.method === 'POST') {
      previews.push(JSON.parse(String(init.body)));
      return json({ schema_version: 'frisket.action_preview.v1', preview_id: 'table', total: null }, 202);
    }
    if (url.endsWith('/actions/v1/preview/table')) return json({
      schema_version: 'frisket.action_preview.v1', preview_id: 'table', status: 'done',
      progress: { done: 2, total: null }, result: { kind: 'table',
        columns: [{ name: 'Name', column_type: 'text', format: null, hidden: false, overwrites_column_id: null }],
        rows: [{ Name: { value: 'Second' } }, { Name: { value: 'First' } }], sampled: 2, total: null,
      },
    });
    if (url.endsWith('/actions/v1/run') && init?.method === 'POST') {
      runs.push(JSON.parse(String(init.body)));
      // A real admission refusal, not a fabricated successful receipt.
      return json({ schema_version: 'frisket.action_result.v1', status: 'error',
        errors: [{ code: 'test_refusal', message: 'Execution refused by the server.' }] }, 400);
    }
    if (url.endsWith('/sheets')) return json([]);
    return json({});
  }));
  render(<WorkspaceStoresProvider projectId={PROJECT.id}><Probe /></WorkspaceStoresProvider>);
  await waitFor(() => expect(latest?.sheetsLoaded).toBe(true));
  expect(latest?.mainViewModel.sheet).toBeUndefined();
  act(() => { latest!.executeRegisteredAction(request, 'preview'); });
  await screen.findByRole('table', { name: 'Preview sample' });
  expect(screen.getAllByRole('row').slice(1).map((row) => row.textContent))
    .toEqual(['Second', 'First']);
  expect(screen.getByTestId('preview-tab-stats')).toHaveTextContent(/^Preview · 2 rows$/);
  expect(previews).toEqual([request]);
  act(() => { latest!.mainViewModel.runPreviewForReal(); });
  await waitFor(() => expect(runs).toHaveLength(1));
  expect(runs[0]).toEqual({ ...request, idempotency_key: expect.any(String) });
  expect(runs[0].idempotency_key).not.toBe(request.idempotency_key);
  expect(screen.queryByRole('table', { name: 'Preview sample' })).toBeNull();
});
