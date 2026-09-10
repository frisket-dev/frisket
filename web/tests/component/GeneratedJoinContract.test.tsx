// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it } from 'vitest';
import type { ActionParamResolution, GeneratedActionDraft } from '../../src/api/types';
import { joinApi, joinLeft, joinRight, renderJoinForm } from '../support/renderJoinForm';
import { servedActionCatalog, servedActionCatalogPython } from '../support/servedActionCatalog';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { isGeneratedActionCatalogEntry } from '../../src/api/types';

const columns = [{ side: 'right', column: 'city' }, { side: 'left', column: 'city' },
  { side: 'right', column: 'city' }];
const params = { right: { sheet_id: 2 }, join_keys: [{ left_column: 'code', right_column: 'code' }],
  columns, indicator: true };
const semantic = { source: 'city', target: { sheet_id: 2, column: 'city' }, carry: ['state'] };
// Obtain the two actual discovery schemas from the public service once. No
// browser key allocator or materialized row fixture can substitute for this.
const requests = [
  { action_id: 'derive.join', params },
  { action_id: 'derive.join', params: { ...params, columns: columns.slice(0, 2) } },
  { action_id: 'join.semantic', params: semantic },
].map((request) => ({
  ...request,
  scope: { kind: 'sheet_rows', sheet_id: 1 },
  sheet_name: 'Discovery',
}));
const root = resolve(process.cwd(), '..');
const discovery = spawnSync(servedActionCatalogPython(), ['-c', `
import json, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from frisket.engine.store import Project
from frisket.server.services.action_param_validation import ActionParamValidationService
with tempfile.TemporaryDirectory() as tmp:
    project = Project.create(Path(tmp) / "join.frisket")
    try:
        for name, names in [("Cities", ["code", "city", "state"]), ("Zones", ["code", "city", "zone"])]:
            sheet = project.add_sheet(name)
            columns = {key: project.add_column(sheet, key, type="text") for key in names}
            project.add_rows(sheet, [{key: "example" for key in names}], columns)
        service = ActionParamValidationService(SimpleNamespace(edition="local", get=lambda _: project))
        print(json.dumps([service.validate_params("project", request) for request in json.load(sys.stdin)]))
    finally:
        project.close()
`], { cwd: root, input: JSON.stringify(requests), encoding: 'utf8',
  env: { ...process.env, PYTHONPATH: resolve(root, 'src'), FRISKET_CHECKLOG_DISABLE: '1' } });
if (discovery.status !== 0) throw new Error(discovery.stderr || 'Join schema discovery failed');
const schemas = JSON.parse(discovery.stdout) as ActionParamResolution[];
for (const schema of schemas) {
  if (Object.keys(schema.diagnostics).length) throw new Error(JSON.stringify(schema.diagnostics));
}
afterEach(cleanup);
beforeEach(() => joinApi.listSheets.mockReset().mockResolvedValue([joinLeft, joinRight]));
async function ready() { await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled()); }
function open(initial: GeneratedActionDraft) {
  return renderJoinForm({ initialDraft: initial, resolveParams: async ({ params: current }) =>
    schemas[(current.columns as unknown[])?.length === 3 ? 0 : 1] });
}

it('uses actual ordinary discovery keys and prunes only removed renamed projections after a successful edit', async () => {
  expect(schemas[0].logical_outputs.map(({ key }) => key)).toEqual([
    'code', 'city_right', 'city_left', 'city_right_2', '_merge',
  ]);
  const view = open({ ...requests[0], sheet_name: 'Joined', output_names: {
    code: 'Join key', city_right: 'Registry', city_left: 'Reported', city_right_2: 'Review copy',
  } });
  await ready();
  expect(screen.getByTestId('field-output-city_right_2')).toHaveValue('Review copy');
  fireEvent.click(screen.getByRole('button', { name: 'Remove projection 3' }));
  await waitFor(() => expect(screen.queryByTestId('field-output-city_right_2')).not.toBeInTheDocument());
  await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0].output_names).toEqual({
    code: 'Join key', city_right: 'Registry', city_left: 'Reported',
  });
  expect(view.onExecute.mock.calls[0][0].params.columns).toEqual(columns.slice(0, 2));
});

it('surfaces unknown saved names with explicit repair instead of silently deleting saved intent', async () => {
  const view = open({ ...requests[1], sheet_name: 'Joined', output_names: {
    city_left: 'Reported', removed_key: 'Saved unknown name',
  } });
  await waitFor(() => expect(screen.getByTestId('stale-output-names')).toHaveTextContent('removed_key'));
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  expect(view.onExecute).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Remove unavailable output names' })); await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0].output_names).toEqual({ city_left: 'Reported' });
});

it('renders actual semantic child source/carry keys alongside the shared match outputs', async () => {
  expect(schemas[2].logical_outputs.map(({ key }) => key)).toEqual([
    'match_value', 'match_score', 'matched_row_id', 'source', 'carry.state',
  ]);
  const view = renderJoinForm({ initialDraft: { ...requests[2], sheet_name: 'Matches' },
    resolveParams: async () => schemas[2] }, 'join.semantic');
  await ready();
  fireEvent.change(screen.getByTestId('field-output-source'), { target: { value: 'Original city' } });
  fireEvent.change(screen.getByTestId('field-output-carry.state'), { target: { value: 'Region' } });
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0].output_names).toEqual({ source: 'Original city', 'carry.state': 'Region' });
});

it('does not carry a transient custom editor problem into another generated action', async () => {
  joinApi.listSheets.mockReturnValueOnce(new Promise(() => {}));
  const view = open({ ...requests[0], sheet_name: 'Joined' });
  expect(screen.getByTestId('generated-editor-problem')).toHaveTextContent('Loading');
  const raw = servedActionCatalog().actions.find((entry) => entry.kind === 'map.regex_extract')!;
  if (!isGeneratedActionCatalogEntry(raw)) throw new Error('Missing generated regex');
  view.rerender(view.rerenderForm({ catalogEntry: raw,
    actionTemplate: generatedActionTemplateFromCatalogEntry(raw)!,
    resolveParams: async () => ({ diagnostics: {}, logical_outputs: raw.ui_hints.logical_outputs }) }));
  expect(screen.queryByTestId('tabular-join-form')).not.toBeInTheDocument();
  expect(screen.queryByTestId('generated-editor-problem')).not.toBeInTheDocument();
});
