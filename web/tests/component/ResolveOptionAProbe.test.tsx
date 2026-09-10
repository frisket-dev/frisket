// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { actionTemplatesFromCatalog } from '../../src/actions/model';
import type { ActionCatalogEntry, ActionCatalogPayload, GeneratedActionRequest } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ActionPanel } from '../../src/components/ActionPanel';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { completeCatalogPayload, sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';

const api = createProjectApi('test-project');
const structuredProperties = {
  'resolve.substitute': { mapping: { type: 'object' } },
  'resolve.replace': { rules: { type: 'array', items: { type: 'object' } } },
  'resolve.combine': {
    groups: { type: 'array', items: { type: 'object' } },
    unmatched_value: { anyOf: [{ type: 'string' }, { type: 'null' }], default: null },
  },
} as const;
const entries = Object.entries(structuredProperties).map(([kind, structured]) => ({
  kind,
  authoring_contract_version: 1,
  title: kind,
  description: kind,
  input_schema: {
    type: 'object', additionalProperties: false,
    required: ['source', Object.keys(structured)[0]],
    properties: {
      source: { type: 'string' },
      unmatched: { type: 'string', enum: kind === 'resolve.combine'
        ? ['keep', 'null', 'value'] : ['keep', 'null'], default: 'keep' },
      ...structured,
    },
  },
  output_schema: { type: 'object' },
  required_capabilities: ['project:write'],
  cost_policy: { kind: 'none', requires_confirmation: false },
  writes_project: true,
  execution_mode: 'whole_project',
  async_mode: 'sync',
  row_scope_policy: { kind: 'sheet_rows', selectors: ['all_rows'] },
  ui_hints: {
    form: 'generated', category: 'resolve', form_params: [],
    semantic_controls: { source: 'column' },
    logical_outputs: [{ key: 'cleaned', column_type: 'text' }],
    primary_fields: ['source'],
    source_requirements: [{
      id: 'source', mode: 'column', param: 'source', label: 'Column', min: 1, max: 1,
      accepted_column_types: ['text', 'category', 'link'], accepted_cell_kinds: ['text'],
    }],
  },
})) as unknown as ActionCatalogEntry[];
const catalog: ActionCatalogPayload = completeCatalogPayload(entries);
const templates = actionTemplatesFromCatalog(catalog);
const sheet = sheetMeta([
  columnDef({ id: '11', name: 'employer', type: 'text' }),
  columnDef({ id: '12', name: 'notes', type: 'text' }),
  columnDef({ id: '13', name: 'score', type: 'number' }),
], { id: '7', name: 'People', rowCount: 2542 });
const columnPreview = {
  sheetId: '7', columnId: '11', inputColumn: 'employer', totalRows: 2542,
  distinct: 3, missing: 0,
  values: [
    { value: 'Acme Corp', count: 312 },
    { value: 'ACME CORP.', count: 201 },
    { value: 'Hooli', count: 110 },
  ],
  offset: 0, limit: 2000, truncated: false,
  valueHash: 'snapshot:fresh-column', search: null, distribution: null,
};

const mountedStores: WorkspaceStores[] = [];
afterEach(() => {
  cleanup();
  for (const stores of mountedStores.splice(0)) stores.dispose();
  vi.restoreAllMocks();
});

function mount(kind: string, inspectProposal?: Record<string, unknown>, running = false) {
  const stores = createWorkspaceStores(`resolve-probe-${kind}`, api);
  mountedStores.push(stores);
  stores.actionCatalog.store.set(() => ({
    status: 'ready', error: null, catalog, resolvedTemplates: templates, version: 1,
  }));
  const onExecute = vi.fn();
  vi.spyOn(api, 'resolveActionParams').mockResolvedValue({
    diagnostics: {}, logical_outputs: [{ key: 'cleaned', column_type: 'text' }],
  });
  render(
    <WorkspaceStoresContext.Provider value={stores}>
      <ActionPanel sheet={sheet} running={running}
        routeActionKind={inspectProposal ? undefined : kind} routeActionLaunchId={17}
        inspectProposal={inspectProposal
          ? { seq: 18, title: 'Saved resolve', spec: inspectProposal } : undefined}
        onRun={vi.fn()} onExecuteRegisteredAction={onExecute} />
    </WorkspaceStoresContext.Provider>,
  );
  return onExecute;
}

async function authorFresh(kind: 'substitute' | 'replace' | 'combine') {
  vi.spyOn(api, 'columnValuesPreview').mockResolvedValue(columnPreview);
  vi.spyOn(api, 'replaceRulesPreview').mockImplementation(async (input) => ({
    sheetId: input.sheetId, columnId: '11', totalRows: 2542,
    ruleCounts: input.rules.map((_, index) => ({ index, matchedRows: 100, matchedValues: 1 })),
    unmatchedRows: 2442, testResult: null, valueHash: 'snapshot:fresh-rules',
  }));
  const onExecute = mount(`resolve.${kind}`);
  await screen.findByTestId(`resolve-${kind}-form`);
  if (kind === 'substitute') {
    await screen.findByTestId('resolve-substitute-facts');
    fireEvent.change(screen.getAllByTestId('resolve-substitute-target-input')[0], {
      target: { value: 'Acme Corporation' },
    });
  } else if (kind === 'replace') {
    const rule = screen.getByTestId('resolve-replace-rule');
    fireEvent.change(within(rule).getByTestId('resolve-replace-rule-pattern'), {
      target: { value: 'acme' },
    });
    fireEvent.change(within(rule).getByTestId('resolve-replace-rule-target'), {
      target: { value: 'Acme Corporation' },
    });
  } else {
    await screen.findByTestId('resolve-combine-unassigned');
    const rows = within(screen.getByTestId('resolve-combine-unassigned'))
      .getAllByTestId('resolve-value-row');
    fireEvent.click(rows[0]);
    fireEvent.click(rows[1]);
    fireEvent.click(screen.getByTestId('resolve-combine-new-bucket'));
  }
  await waitFor(() => expect(screen.getByTestId('resolve-apply')).toBeEnabled());
  fireEvent.click(screen.getByTestId('resolve-apply'));
  await waitFor(() => expect(onExecute).toHaveBeenCalledTimes(1));
  return onExecute.mock.calls[0][0] as GeneratedActionRequest;
}

describe('generated Resolve host/body boundary', () => {
  it('keeps the common host around every rich Params body', async () => {
    vi.spyOn(api, 'columnValuesPreview').mockResolvedValue(columnPreview);
    mount('resolve.substitute');
    await screen.findByTestId('resolve-substitute-form');
    expect(screen.getByTestId('action-form-title')).toHaveTextContent('resolve.substitute');
    expect(screen.getByTestId('resolve-substitute-column')).toHaveValue('employer');
    expect(screen.getByTestId('field-output-cleaned')).toHaveValue('employer_clean');
    expect(screen.getByTestId('generated-action-preview')).toBeInTheDocument();
    expect(screen.getByTestId('resolve-apply')).toHaveTextContent('Apply mapping');
    expect(screen.getByTestId('action-drawer-close')).toBeInTheDocument();
  });

  it('sends each rich body through one generated request host', async () => {
    const expected = {
      substitute: { source: 'employer', mapping: { 'Acme Corp': 'Acme Corporation' }, unmatched: 'keep' },
      replace: { source: 'employer', rules: [{ match: 'contains', pattern: 'acme',
        target: 'Acme Corporation', case_sensitive: false }], unmatched: 'keep' },
      combine: { source: 'employer', groups: [{ canonical: 'Acme Corp',
        members: ['Acme Corp', 'ACME CORP.'] }], unmatched: 'keep' },
    } as const;
    for (const kind of ['substitute', 'replace', 'combine'] as const) {
      const request = await authorFresh(kind);
      expect(request).toMatchObject({
        action_id: `resolve.${kind}`,
        scope: { kind: 'sheet_rows', sheet_id: 7 },
        params: expected[kind],
        output_names: { cleaned: 'employer_clean' },
      });
      expect(request).not.toHaveProperty('input_snapshot');
      expect(request).not.toHaveProperty('replace_existing');
      cleanup();
    }
  });

  it('surfaces continuous server diagnostics through the host', async () => {
    vi.spyOn(api, 'columnValuesPreview').mockResolvedValue(columnPreview);
    mount('resolve.substitute');
    vi.mocked(api.resolveActionParams).mockImplementation(async ({ params }) => ({
      diagnostics: Object.keys(params.mapping as object ?? {}).length
        ? { mapping: { ok: false, message: 'Mapping is no longer valid.' } } : {},
      logical_outputs: [{ key: 'cleaned', column_type: 'text' }],
    }));
    await screen.findByTestId('resolve-substitute-facts');
    fireEvent.change(screen.getAllByTestId('resolve-substitute-target-input')[0], {
      target: { value: 'Acme Corporation' },
    });
    expect(await screen.findByRole('alert')).toHaveTextContent('Mapping is no longer valid.');
    expect(screen.getByTestId('resolve-apply')).toBeDisabled();
  });

  it('hydrates saved Params and output names without destructive consent', async () => {
    vi.spyOn(api, 'columnValuesPreview').mockResolvedValue({
      ...columnPreview, inputColumn: 'notes', columnId: '12',
    });
    const params = {
      source: 'notes', groups: [{ canonical: 'Acme', members: ['Acme Corp', 'ACME CORP.'] }],
      unmatched: 'value', unmatched_value: 'Other',
    };
    const onExecute = mount('resolve.combine', {
      action_id: 'resolve.combine', scope: { kind: 'sheet_rows', sheet_id: 7 }, params,
      output_names: { cleaned: 'saved_output' },
    });
    await screen.findByTestId('resolve-combine-unassigned');
    expect(screen.getByTestId('resolve-combine-column-select')).toHaveValue('notes');
    expect(screen.getByTestId('field-output-cleaned')).toHaveValue('saved_output');
    expect(screen.getByTestId('resolve-combine-bucket')).toHaveTextContent('Acme');
    await waitFor(() => expect(screen.getByTestId('resolve-apply')).toBeEnabled());
    fireEvent.click(screen.getByTestId('resolve-apply'));
    await waitFor(() => expect(onExecute).toHaveBeenCalledTimes(1));
    expect(onExecute.mock.calls[0][0]).toMatchObject({ params, output_names: { cleaned: 'saved_output' } });
    expect(onExecute.mock.calls[0][0]).not.toHaveProperty('replace_existing');
  });

  it('host running state disables lifecycle controls', async () => {
    vi.spyOn(api, 'columnValuesPreview').mockResolvedValue(columnPreview);
    mount('resolve.substitute', undefined, true);
    await screen.findByTestId('resolve-substitute-column');
    expect(screen.getByTestId('generated-action-preview')).toBeDisabled();
    expect(screen.getByTestId('resolve-apply')).toBeDisabled();
  });
});
