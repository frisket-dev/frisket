// @vitest-environment jsdom
//
// Lane-local mount helper (test-cutover-ui-f1) for the typed GeneratedActionForm
// backed by the backend-served catalog. Every test in this lane mounts through
// here so the wire asserted is the typed request shape
// (action_id / scope / params / output_names / idempotency_key), never a
// hand-built ActionTemplate or a retired recipe schema.

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, vi } from 'vitest';

import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import * as api from '../../src/api/open';
import { createProjectApi } from '../../src/api/real';
import type {
  ActionParamResolution,
  GeneratedActionCatalogEntry,
  GeneratedActionDraft,
  GeneratedActionRequest,
  RunEstimate,
  SheetMeta,
} from '../../src/api/types';
import { isGeneratedActionCatalogEntry } from '../../src/api/types';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { createWorkspaceStores } from '../../src/state/createWorkspaceStores';
import { servedActionCatalog } from '../support/servedActionCatalog';

/** The served catalog entry for `kind`, cloned, with every engine selectable.
 *  Engine availability is a runtime fact the fixture script cannot observe;
 *  the form's own unavailable-engine handling is covered elsewhere. */
export function typedCatalogEntry(kind: string): GeneratedActionCatalogEntry {
  const entry = structuredClone(servedActionCatalog().actions.find((candidate) => candidate.kind === kind));
  if (!entry || !isGeneratedActionCatalogEntry(entry)) {
    throw new Error(`${kind} is not a served generated action`);
  }
  entry.ui_hints.engines = entry.ui_hints.engines?.map((engine) => (
    { ...engine, available: true, error: undefined }
  ));
  return entry;
}

type ResolveRequest = Pick<GeneratedActionRequest, 'action_id' | 'scope' | 'params'>;

/** The server owns logical-output resolution for dynamic-output kinds. This
 *  mirrors its declared rule for the kinds in this lane so the form has real
 *  outputs to name; the resolution itself is exercised by backend tests. */
export function servedLogicalOutputs(
  kind: string,
  params: Record<string, unknown>,
): ActionParamResolution['logical_outputs'] {
  if (kind === 'map.translate') {
    return [
      { key: 'translation', column_type: 'text' },
      ...(params.save_detected_language === true
        ? [{ key: 'detected_language', column_type: 'category' }] : []),
    ];
  }
  const fields = Array.isArray(params.fields) ? params.fields : [];
  return fields.flatMap((field) => (
    field && typeof field === 'object' && typeof (field as { name?: unknown }).name === 'string'
      ? [{ key: (field as { name: string }).name,
          column_type: String((field as { type?: unknown }).type ?? 'text') }]
      : []
  ));
}

export function mockLocalProviders() {
  return vi.spyOn(api, 'listProviders').mockResolvedValue({
    schemaVersion: 'frisket.providers.v1',
    tier: 'local',
    providers: [{ id: 'test', label: 'Test', kind: 'platform_api', configured: true, source: 'env',
      hint: null, models: [{ id: 'test/model', label: 'Test model', price: null }] }],
  });
}

export interface MountTypedFormOptions {
  kind: string;
  sheet: SheetMeta | null;
  initialDraft?: GeneratedActionDraft;
  initialSourceColumn?: string;
  selectedRowIds?: string[];
  hasExactRowScopeInitializer?: boolean;
  resolveParams?: (request: ResolveRequest) => Promise<ActionParamResolution>;
  estimateAction?: (request: GeneratedActionRequest) => Promise<RunEstimate>;
  listMcpServers?: Array<{ id: string; name: string; enabled: boolean; last_discovered_tool_count?: number }>;
}

export function mountTypedForm(options: MountTypedFormOptions) {
  const entry = typedCatalogEntry(options.kind);
  const template = generatedActionTemplateFromCatalogEntry(entry);
  if (!template) throw new Error(`No generated template for ${options.kind}`);
  const projectApi = createProjectApi(`cutover-ui-f1:${options.kind}`);
  vi.spyOn(projectApi, 'listMcpServers').mockResolvedValue(
    (options.listMcpServers ?? []) as Awaited<ReturnType<typeof projectApi.listMcpServers>>,
  );
  const stores = createWorkspaceStores(`cutover-ui-f1:${options.kind}`, projectApi);
  const resolveParams = vi.fn(options.resolveParams ?? (async (request: ResolveRequest) => ({
    diagnostics: {},
    logical_outputs: entry.ui_hints.dynamic_outputs === true
      ? servedLogicalOutputs(entry.kind, request.params)
      : entry.ui_hints.logical_outputs,
  })));
  const estimateAction = vi.fn(options.estimateAction ?? (async () => ({
    cost: 0, rows: options.sheet.rowCount, billed_cost: 0, policy_id: 'frisket.identity.v1',
  })));
  const onExecute = vi.fn();
  const utils = render(
    <WorkspaceStoresContext.Provider value={stores}>
      <GeneratedActionForm
        catalogEntry={entry}
        actionTemplate={template}
        sheet={options.sheet}
        selectedRowIds={options.selectedRowIds}
        hasExactRowScopeInitializer={options.hasExactRowScopeInitializer}
        initialSourceColumn={options.initialSourceColumn}
        initialDraft={options.initialDraft}
        running={false}
        resolveParams={resolveParams}
        estimateAction={estimateAction}
        onExecute={onExecute}
        onClose={vi.fn()}
      />
    </WorkspaceStoresContext.Provider>,
  );
  return {
    ...utils,
    entry,
    template,
    onExecute,
    resolveParams,
    estimateAction,
    dispose: () => stores.dispose(),
  };
}

export function lastExecutedRequest(onExecute: { mock: { calls: unknown[][] } }): GeneratedActionRequest {
  const calls = onExecute.mock.calls;
  expect(calls.length).toBeGreaterThan(0);
  return calls[calls.length - 1][0] as GeneratedActionRequest;
}

/** Wait for the typed form's own Run gate, then press Run. */
export async function pressRun(): Promise<void> {
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-run'));
}

/** Pick an execution choice through the combined engine/model picker. */
export async function chooseEngine(engineId: string): Promise<void> {
  const user = userEvent.setup();
  const modelPicker = screen.queryByTestId('model-picker-button');
  if (modelPicker) {
    await user.click(modelPicker);
    const value = engineId === 'llm' ? 'test/model' : `engine:${engineId}`;
    await user.type(screen.getByTestId('model-picker-search'), value);
    const testId = `model-option-${value.replace(/[^a-z0-9]+/gi, '-').replace(/^-+|-+$/g, '').toLowerCase()}`;
    await user.click(await screen.findByTestId(testId));
    return;
  }

  await user.click(screen.getByTestId('engine-picker-button'));
  await user.type(screen.getByTestId('engine-picker-search'), engineId);
  await user.click(await screen.findByTestId(`engine-option-${engineId}`));
}
