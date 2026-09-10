// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as apiModule from '../../src/api/open';
import type { GeneratedActionDraft } from '../../src/api/types';
import { decodeSavedActionSpec, encodeSavedActionSpec } from '../../src/actions/savedActionSpec';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ActionPanel } from '../../src/components/ActionPanel';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { PYTHON_CATALOG, PYTHON_SHEET, PYTHON_TEMPLATES, renderPythonForm,
  resolvePythonParams } from '../support/pythonActionFixture';

let stores: WorkspaceStores | null = null;
beforeEach(() => {
  vi.spyOn(apiModule, 'getRuntimeConfig').mockResolvedValue({
    cache_mode: 'replay', live_calls_possible: true, cache_mode_editable: false,
    email_from_address: null, email_from_name: null, recipe_fence_posture: 'enforced',
  });
});
afterEach(() => { cleanup(); stores?.dispose(); stores = null; vi.restoreAllMocks(); });

function saved(hiddenOnly = false): GeneratedActionDraft {
  return {
    action_id: 'map.python', scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101, 103] },
    params: {
      input_columns: ['title', 'score'],
      code: 'result = {"length": len(str(row["title"])), "items": [{"name": "A"}]}',
      return_schema: { type: 'object', required: ['length', 'items'], properties: {
        length: { type: 'integer' }, items: { type: 'array', items: { type: 'object' } },
      } },
      output_routes: [
        ...(hiddenOnly ? [] : [{ name: 'length', path: '$.length',
          target: { kind: 'column', type: 'integer' } }]),
        { name: 'entities', path: '$.items', target: { kind: 'named_result',
          schema: 'entity_list', may_feed: ['derive.table_from_list'] } },
        { name: 'proof', path: '$', target: { kind: 'receipt_evidence', retention: 'pinned' } },
      ],
    }, output_names: hiddenOnly ? {} : { length: 'Measured length' },
  };
}

describe('typed Python consumer boundary', () => {
  it('edits saved schema and routes as structured JSON and blocks unfinished JSON', async () => {
    const draft = saved();
    const onExecute = vi.fn();
    const resolveParams = vi.fn(resolvePythonParams);
    renderPythonForm({ initialDraft: draft, onExecute, resolveParams });
    const schemaField = await screen.findByTestId('field-return_schema');
    const routesField = screen.getByTestId('field-output_routes');
    expect(schemaField).toHaveValue(JSON.stringify(draft.params.return_schema, null, 2));
    expect(routesField).toHaveValue(JSON.stringify(draft.params.output_routes, null, 2));
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.change(schemaField, { target: { value: '{' } });
    expect(schemaField).toHaveValue('{');
    expect(screen.getByText('Enter a valid JSON object.')).toBeInTheDocument();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(screen.getByTestId('generated-action-preview')).toBeDisabled();
    const returnSchema = { type: 'object', properties: { count: { type: 'integer' } } };
    const outputRoutes = [
      { name: 'count', path: '$.count', target: { kind: 'column', type: 'integer' } },
      { name: 'proof', path: '$', target: { kind: 'receipt_evidence', retention: 'pinned' } },
    ];
    fireEvent.change(schemaField, { target: { value: JSON.stringify(returnSchema) } });
    fireEvent.change(routesField, { target: { value: '[' } });
    expect(screen.getByText('Enter a valid JSON array.')).toBeInTheDocument();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    fireEvent.change(routesField, { target: { value: JSON.stringify(outputRoutes) } });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    expect(resolveParams).toHaveBeenLastCalledWith(expect.objectContaining({
      params: expect.objectContaining({ return_schema: returnSchema, output_routes: outputRoutes }),
    }));
    expect(screen.queryByTestId('field-output-proof')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('generated-action-preview'));
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledTimes(2);
    for (const [request] of onExecute.mock.calls) {
      expect(request.params).toEqual({ ...draft.params,
        return_schema: returnSchema, output_routes: outputRoutes });
      const { action_id, scope, params, output_names } = request;
      const edited = { action_id, scope, params, output_names };
      expect(encodeSavedActionSpec(decodeSavedActionSpec(PYTHON_CATALOG, edited))).toEqual(edited);
    }
  });

  it('launches canonical Params with semantic selected inputs and generic output names', async () => {
    const onExecute = vi.fn();
    renderPythonForm({ initialSourceColumn: 'score', selectedRowIds: ['101', '103'], onExecute });
    const output = await screen.findByTestId('field-output-computed');
    fireEvent.change(output, { target: { value: 'Computed data' } });
    fireEvent.change(screen.getByTestId('field-code'), {
      target: { value: 'result = {"score": row["score"]}' },
    });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
      action_id: 'map.python', scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101, 103] },
      params: { input_columns: ['score'], code: 'result = {"score": row["score"]}',
        return_schema: { type: 'object' },
        output_routes: [{ name: 'computed', path: '$', target: { kind: 'column', type: 'json' } }],
      }, output_names: { computed: 'Computed data' },
    }), 'run');
  });

  it.each([false, true])('preserves rich saved routes with hidden-only=%s across edit, preview and run',
    async (hiddenOnly) => {
      const draft = saved(hiddenOnly);
      expect(encodeSavedActionSpec(decodeSavedActionSpec(PYTHON_CATALOG, draft))).toEqual(draft);
      stores = createWorkspaceStores('python-saved');
      stores.actionCatalog.store.set(() => ({ status: 'ready', error: null,
        catalog: PYTHON_CATALOG, resolvedTemplates: PYTHON_TEMPLATES, version: 1 }));
      vi.spyOn(stores.projectApi, 'resolveActionParams').mockImplementation(resolvePythonParams);
      const onExecute = vi.fn();
      render(<WorkspaceStoresContext.Provider value={stores}>
        <ActionPanel sheet={PYTHON_SHEET} running={false} onRun={vi.fn()}
          onExecuteRegisteredAction={onExecute}
          inspectProposal={{ seq: 1, title: 'Saved Python', spec: draft }} />
      </WorkspaceStoresContext.Provider>);
      expect(await screen.findByTestId('field-code')).toHaveValue(draft.params.code);
      await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
      if (!hiddenOnly) {
        expect(screen.getByTestId('field-output-length')).toHaveValue('Measured length');
        fireEvent.change(screen.getByTestId('field-output-length'), { target: { value: 'Length edited' } });
      }
      expect(screen.queryByTestId('field-output-entities')).not.toBeInTheDocument();
      expect(screen.queryByTestId('field-output-proof')).not.toBeInTheDocument();
      const code = `${draft.params.code}\n# edited`;
      fireEvent.change(screen.getByTestId('field-code'), { target: { value: code } });
      await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
      fireEvent.click(screen.getByTestId('generated-action-preview'));
      fireEvent.click(screen.getByTestId('generated-action-run'));
      expect(onExecute).toHaveBeenCalledTimes(2);
      for (const [request] of onExecute.mock.calls) expect(request).toMatchObject({
        ...draft, params: { ...draft.params, code },
        output_names: hiddenOnly ? {} : { length: 'Length edited' },
      });
      expect(onExecute.mock.calls[0][1]).toBe('preview');
      expect(onExecute.mock.calls[1][1]).toBe('run');
    });
});
