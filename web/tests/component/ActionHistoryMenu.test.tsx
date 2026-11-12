// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createProjectApi } from '../../src/api/real';
import type { ActionHistoryItem } from '../../src/api/actionHistory';
import { ActionHistoryMenu } from '../../src/components/action-panel/ActionHistoryMenu';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const projectApi = createProjectApi('history');
const { render } = createWorkspaceTestHarness({ projectId: 'history', api: { projectApi } });
const item: ActionHistoryItem = { id: 'submission:12', action_id: 'map.extract',
  sheet_id: 7, sheet_name: 'Court filings', submitted_at: '2026-11-12T10:12:00Z',
  status: 'not_started', run_id: null };
const draft = { action_id: 'map.extract', scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [11, 12] },
  params: { source: ['story'], model: 'test/model', instruction: 'Recover this prompt',
    fields: [{ name: 'party', type: 'text', description: 'Named party' }] },
  output_names: { party: 'plaintiff' }, sheet_name: null };
function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}
function mount(choose = vi.fn()) {
  const trigger = document.createElement('button');
  trigger.dataset.historyTestTrigger = 'true'; document.body.append(trigger);
  const close = vi.fn();
  const view = render(<ActionHistoryMenu actionId="map.extract" trigger={trigger} onChoose={choose} onClose={close} />);
  return { ...view, choose, close };
}
afterEach(() => {
  cleanup(); document.querySelectorAll('[data-history-test-trigger]').forEach(element => element.remove());
  vi.unstubAllGlobals(); vi.restoreAllMocks();
});

describe('action history at the HTTP boundary', () => {
  it('pages submissions and old runs, restores saved settings, and never executes', async () => {
    const requests: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.method ?? 'GET').toBe('GET');
      const url = new URL(String(input), 'http://localhost'); requests.push(url.pathname + url.search);
      if (url.pathname.endsWith('/actions/history')) {
        expect(url.searchParams.get('action_id')).toBe('map.extract');
        expect(url.searchParams.get('limit')).toBe('20');
        const offset = Number(url.searchParams.get('offset'));
        return response({ items: [offset ? { ...item, id: 'run:42', status: 'failed', run_id: 42 } : item],
          total: 21, offset, limit: 20 });
      }
      if (decodeURIComponent(url.pathname).endsWith('/actions/history/submission:12')) {
        return response({ item, draft, unavailable_reason: null });
      }
      throw new Error(`Unexpected request ${url.pathname}`);
    }));
    const { choose, close } = mount();
    expect(await screen.findByText('Not started')).toBeInTheDocument();
    expect(screen.getByText('Court filings')).toBeInTheDocument();
    expect(screen.queryByText('Recover this prompt')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('menuitem', { name: 'Older' }));
    expect(await screen.findByText('Failed')).toBeInTheDocument();
    expect(requests.some(url => url.includes('offset=20'))).toBe(true);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Newer' }));
    fireEvent.click(await screen.findByTestId('action-history-choose-submission:12'));
    await waitFor(() => expect(choose).toHaveBeenCalledWith({
      action_id: draft.action_id, scope: draft.scope, params: draft.params, output_names: draft.output_names,
    }));
    expect(requests.every(url => url.includes('/actions/history'))).toBe(true);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(close).toHaveBeenCalled();
  });

  it('shows unavailable settings without restoring or executing and allows retry', async () => {
    let details = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://localhost');
      if (url.pathname.endsWith('/actions/history')) return response({ items: [item], total: 1, offset: 0, limit: 20 });
      details++;
      return response({ item, draft: null, unavailable_reason: 'The source sheet was deleted.' });
    }));
    const { choose } = mount();
    fireEvent.click(await screen.findByTestId('action-history-choose-submission:12'));
    expect(await screen.findByRole('alert')).toHaveTextContent('The source sheet was deleted.');
    expect(choose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('action-history-choose-submission:12'));
    await waitFor(() => expect(details).toBe(2));
  });

  it.each([{ kind: 'project' }, { kind: 'sheet_rows', sheet_id: 7, row_ids: [] }])(
    'preserves saved scope %j when normalizing a detail response', async scope => {
      vi.stubGlobal('fetch', vi.fn(async () => response({ item,
        draft: { ...draft, scope }, unavailable_reason: null })));
      expect((await projectApi.getActionHistoryEntry(item.id)).draft?.scope).toEqual(scope);
    },
  );

  it('does not restore after the menu was closed during a detail request', async () => {
    let resolveDetail!: (result: Response) => void;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (new URL(String(input), 'http://localhost').pathname.endsWith('/actions/history')) {
        return response({ items: [item], total: 1, offset: 0, limit: 20 });
      }
      return new Promise<Response>(resolve => { resolveDetail = resolve; });
    }));
    const { choose, unmount } = mount();
    fireEvent.click(await screen.findByTestId('action-history-choose-submission:12'));
    await screen.findByText('Loading settings…'); unmount();
    resolveDetail(response({ item, draft, unavailable_reason: null }));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(choose).not.toHaveBeenCalled();
  });
});
