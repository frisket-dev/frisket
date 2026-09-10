// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { createProjectApi } from '../../src/api/real';
import type { GeneratedActionDraft, GeneratedActionRequest } from '../../src/api/types';
import { ActionPanel } from '../../src/components/ActionPanel';
import { sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';
import { servedActionCatalog } from '../support/servedActionCatalog';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const projectId = 'join-canonical';
const api = createProjectApi(projectId);
const { render, stores } = createWorkspaceTestHarness({ projectId, api: { projectApi: api } });
const catalog = servedActionCatalog();
const left = sheetMeta([columnDef({ id: '11', name: 'id', type: 'text' })],
  { id: '1', name: 'Orders', rowCount: 4 });
const right = sheetMeta([columnDef({ id: '21', name: 'id', type: 'text' })],
  { id: '2', name: 'Customers', rowCount: 6 });
const saved: GeneratedActionDraft = {
  action_id: 'derive.join', scope: { kind: 'sheet_rows', sheet_id: 1, row_ids: [101, 104] },
  params: { right: { sheet_id: 2 }, join_keys: [{ left_column: 'id', right_column: 'id' }],
    how: 'outer', columns: null, indicator: false, max_output_rows: 1234 },
  sheet_name: 'Saved join', output_names: { id: 'Customer key' },
};

beforeEach(() => {
  stores.actionCatalog.store.set(() => ({ status: 'ready', error: null, catalog,
    resolvedTemplates: actionTemplatesFromCatalog(catalog), version: 1 }));
  vi.spyOn(api, 'listSheets').mockResolvedValue([left, right]);
  vi.spyOn(api, 'resolveActionParams').mockImplementation(async (request) => ({ diagnostics: {},
    logical_outputs: request.action_id === 'join.semantic'
      ? [{ key: 'source', column_type: 'text' }, { key: 'match_value', column_type: 'text' },
        { key: 'match_score', column_type: 'number' }, { key: 'matched_row_id', column_type: 'integer' }]
      : [{ key: 'id', column_type: 'text' }],
  }));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function mount(spec?: GeneratedActionDraft) {
  const posts: GeneratedActionRequest[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    expect(String(input)).toBe(`/api/projects/${projectId}/actions/v1/run`);
    posts.push(JSON.parse(String(init?.body)));
    return new Response(JSON.stringify({
      schema_version: 'frisket.action_result.v1',
      action: { kind: posts.at(-1)!.action_id, action_id: 'joined' },
      status: 'completed', project_id: projectId, run_id: null,
      op_ids: [], outputs: [], errors: [], warnings: [],
    }), { status: 200, headers: { 'content-type': 'application/json' } });
  }));
  const legacyRun = vi.fn();
  const execute = vi.fn((request: GeneratedActionRequest) => { void api.runAction(request); });
  render(<ActionPanel sheet={left} running={false} selectedRowIds={['109']}
    routeActionKind={spec ? undefined : 'derive.join'} routeActionLaunchId={1}
    inspectProposal={spec ? { seq: 1, title: 'Saved join', spec } : undefined}
    onRun={legacyRun} onExecuteRegisteredAction={execute} />);
  return { posts, execute, legacyRun };
}

async function run() {
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-run'));
}

it('launches a canonical route with primary selection and the whole right sheet', async () => {
  const view = mount();
  await waitFor(() => expect(screen.getByTestId('field-join_right_sheet')).toBeEnabled());
  fireEvent.change(screen.getByTestId('field-join_right_sheet'), { target: { value: '2' } });
  if (!screen.queryByTestId('join-key-pair-0')) fireEvent.click(screen.getByTestId('join-key-pair-add'));
  fireEvent.change(screen.getByTestId('field-join_key_left-0'), { target: { value: 'id' } });
  fireEvent.change(screen.getByTestId('field-join_key_right-0'), { target: { value: 'id' } });
  fireEvent.change(await screen.findByTestId('field-output-id'), { target: { value: 'Joined key' } });
  await run();
  await waitFor(() => expect(view.posts).toHaveLength(1));
  expect(view.posts[0]).toMatchObject({ action_id: 'derive.join',
    scope: { kind: 'sheet_rows', sheet_id: 1, row_ids: [109] },
    params: { right: { sheet_id: 2 }, join_keys: [{ left_column: 'id', right_column: 'id' }] },
    sheet_name: 'Joined', output_names: { id: 'Joined key' }, idempotency_key: expect.any(String) });
  expect(view.posts[0]).not.toHaveProperty('kind');
  expect(view.posts[0].params).not.toHaveProperty('right_row_ids');
  expect(view.legacyRun).not.toHaveBeenCalled();
});

it.each(['selected', 'all'] as const)('keeps saved %s primary scope instead of the unrelated current selection', async (mode) => {
  const spec = structuredClone(saved);
  if (mode === 'all') spec.scope = { kind: 'sheet_rows', sheet_id: 1 };
  const view = mount(spec);
  await run();
  await waitFor(() => expect(view.posts).toHaveLength(1));
  expect(view.posts[0]).toEqual({ ...spec, idempotency_key: expect.any(String) });
  expect(view.legacyRun).not.toHaveBeenCalled();
});

it('refuses saved primary-sheet mismatch without remapping the join to the open sheet', async () => {
  const view = mount({ ...saved, scope: { kind: 'sheet_rows', sheet_id: 99, row_ids: [101] } });
  expect(await screen.findByRole('alert')).toHaveTextContent('sheet 99');
  expect(screen.queryByTestId('tabular-join-form')).not.toBeInTheDocument();
  expect(view.execute).not.toHaveBeenCalled();
  expect(view.posts).toEqual([]);
});

it('posts a saved semantic match with source outputs and linked-sheet names in one canonical request', async () => {
  const spec: GeneratedActionDraft = {
    action_id: 'join.semantic', scope: { kind: 'sheet_rows', sheet_id: 1, row_ids: [101, 104] },
    params: { source: 'id', target: { sheet_id: 2, column: 'id' }, carry: [],
      match_threshold: 0.72, confident_threshold: 0.91 },
    sheet_name: 'Matched customers', output_names: { source: 'Original customer',
      match_value: 'Matched customer', match_score: 'Similarity', matched_row_id: 'Customer row' },
  };
  const view = mount(spec);
  await run();
  await waitFor(() => expect(view.posts).toHaveLength(1));
  expect(view.posts[0]).toEqual({ ...spec, idempotency_key: expect.any(String) });
  expect(view.legacyRun).not.toHaveBeenCalled();
});
