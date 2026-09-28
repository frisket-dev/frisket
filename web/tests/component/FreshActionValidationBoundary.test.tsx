// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';

import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { createProjectApi } from '../../src/api/real';
import type {
  ActionParamResolution,
  GeneratedActionDraft,
  GeneratedActionRequest,
  SheetMeta,
} from '../../src/api/types';
import { isGeneratedActionCatalogEntry } from '../../src/api/types';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { sheetMeta } from '../support/actionFormFixtures';
import { aiMeta, columnDef } from '../support/domainFixtures';
import { servedActionCatalog } from '../support/servedActionCatalog';
import { servedActionValidations } from '../support/servedActionValidation';
import { installActionSelectorFixture, selectorProviderCatalog } from '../support/selectorChoicesFixture';

type ValidationRequest = Pick<GeneratedActionRequest, 'action_id' | 'scope' | 'params'>;

const projectId = 'fresh-action-validation';
const scope = { kind: 'sheet_rows' as const, sheet_id: 1 };
const judgeParams = {
  source: ['raw'],
  judged_column: 'answer',
  model: 'test/model',
  guidelines: '',
  include_original_prompt: false,
};
const mcpParams = {
  source: ['story'],
  model: 'test/model',
  instruction: '',
  context: '',
  fields: [{ name: 'value', type: 'text', description: '' }],
  include_confidence: false,
  mcp_server_ids: [],
};
const validationRequests: ValidationRequest[] = [
  { action_id: 'map.judge', scope, params: judgeParams },
  { action_id: 'map.judge', scope, params: { ...judgeParams, guidelines: ' ' } },
  { action_id: 'map.mcp_extract', scope, params: mcpParams },
];

let resolutions: ActionParamResolution[];
beforeAll(() => {
  resolutions = servedActionValidations(validationRequests);
}, 30_000);

let stores: WorkspaceStores | undefined;
afterEach(() => {
  cleanup();
  stores?.dispose();
  stores = undefined;
  vi.restoreAllMocks();
});

const catalog = servedActionCatalog();
const judgeEntry = catalog.actions.find((item) => item.kind === 'map.judge');
const mcpEntry = catalog.actions.find((item) => item.kind === 'map.mcp_extract');
if (!judgeEntry || !isGeneratedActionCatalogEntry(judgeEntry)) throw new Error('Missing generated Judge');
if (!mcpEntry || !isGeneratedActionCatalogEntry(mcpEntry)) throw new Error('Missing generated MCP Extract');
const judgeTemplate = generatedActionTemplateFromCatalogEntry(judgeEntry)!;
const mcpTemplate = generatedActionTemplateFromCatalogEntry(mcpEntry)!;

const judgeSheet: SheetMeta = sheetMeta([
  columnDef({ id: '11', name: 'raw', type: 'text' }),
  columnDef({ id: '12', name: 'answer', type: 'text', ai: aiMeta(), generationManaged: true }),
], { id: '1', name: 'Reviews', rowCount: 2 });
const mcpSheet: SheetMeta = sheetMeta([
  columnDef({ id: '11', name: 'story', type: 'text' }),
], { id: '1', name: 'Stories', rowCount: 2 });

function exactResolver(
  expected: readonly ValidationRequest[],
  served: readonly ActionParamResolution[],
) {
  return vi.fn(async (request: ValidationRequest) => {
    const index = expected.findIndex((candidate) => (
      candidate.action_id === request.action_id
      && JSON.stringify(candidate.scope) === JSON.stringify(request.scope)
      && JSON.stringify(candidate.params) === JSON.stringify(request.params)
    ));
    if (index < 0) throw new Error(`Unexpected validation request: ${JSON.stringify(request)}`);
    return served[index];
  });
}

function installSelector(entry: typeof judgeEntry) {
  installActionSelectorFixture(entry, async () => selectorProviderCatalog(['test/model']));
}

it('uses the backend Judge refusal to keep a fresh error quiet, disable Run, and reveal it after touch', async () => {
  installSelector(judgeEntry);
  const resolveParams = exactResolver(validationRequests.slice(0, 2), resolutions.slice(0, 2));
  expect(resolutions[0].diagnostics.guidelines).toMatchObject({
    ok: false,
    message: 'guidelines must not be blank',
  });

  render(<GeneratedActionForm projectId={projectId} catalogEntry={judgeEntry}
    actionTemplate={judgeTemplate} sheet={judgeSheet} running={false}
    resolveParams={resolveParams} onExecute={vi.fn()} onClose={vi.fn()} />);

  expect(screen.getByTestId('action-form-title')).toHaveTextContent('Judge results');
  expect(screen.getByLabelText('Guidelines')).toHaveClass('form-textarea-autogrow');
  await waitFor(() => expect(resolveParams).toHaveBeenCalledWith(validationRequests[0]));
  expect(screen.queryByText('guidelines must not be blank')).not.toBeInTheDocument();
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Guidelines'), { target: { value: ' ' } });
  expect(await screen.findByTestId('field-guidelines-error'))
    .toHaveTextContent('guidelines must not be blank');
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
});

it('accepts empty MCP context while suppressing only the fresh missing-server refusal', async () => {
  installSelector(mcpEntry);
  const api = createProjectApi(projectId);
  vi.spyOn(api, 'listMcpServers').mockResolvedValue([{
    id: 'server-one', name: 'Local tools', enabled: true,
  }] as Awaited<ReturnType<typeof api.listMcpServers>>);
  stores = createWorkspaceStores(projectId, api);
  const resolveParams = exactResolver([validationRequests[2]], [resolutions[2]]);
  expect(Object.keys(resolutions[2].diagnostics)).toEqual(['mcp_server_ids']);
  expect(resolutions[2].diagnostics.mcp_server_ids).toMatchObject({
    ok: false,
    message: 'Select at least one value.',
  });

  render(<WorkspaceStoresContext.Provider value={stores}>
    <GeneratedActionForm projectId={projectId} catalogEntry={mcpEntry}
      actionTemplate={mcpTemplate} sheet={mcpSheet} running={false}
      resolveParams={resolveParams} onExecute={vi.fn()} onClose={vi.fn()} />
  </WorkspaceStoresContext.Provider>);

  await waitFor(() => expect(resolveParams).toHaveBeenCalledWith(validationRequests[2]));
  expect(screen.queryByText('Select at least one value.')).not.toBeInTheDocument();
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Tools · 0 MCP servers' }));
  const server = await screen.findByRole('checkbox', { name: 'Local tools' });
  fireEvent.click(server);
  fireEvent.click(server);
  expect(await screen.findByText('Select at least one value.')).toBeInTheDocument();
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
});

it('shows the backend Judge refusal immediately for an invalid saved action', async () => {
  installSelector(judgeEntry);
  const initialDraft: GeneratedActionDraft = {
    action_id: 'map.judge',
    scope,
    params: judgeParams,
    output_names: { verdict: 'verdict', judge_note: 'judge_note' },
  };
  const resolveParams = exactResolver([validationRequests[0]], [resolutions[0]]);

  render(<GeneratedActionForm projectId={projectId} catalogEntry={judgeEntry}
    actionTemplate={judgeTemplate} sheet={judgeSheet} initialDraft={initialDraft}
    running={false} resolveParams={resolveParams} onExecute={vi.fn()} onClose={vi.fn()} />);

  expect(await screen.findByTestId('field-guidelines-error'))
    .toHaveTextContent('guidelines must not be blank');
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
});
