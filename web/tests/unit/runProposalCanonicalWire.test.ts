import { afterEach, describe, expect, it, vi } from 'vitest';
import { createProjectApi } from '../../src/api/real';
import type { ActionCatalogEntry, CopilotProposal, CopilotRegisteredActionDraft } from '../../src/api/types';
import type { HttpCopilotReply } from '../../src/generated/openHttpContracts';
import { hasServedActionCatalogPython, servedActionCatalog } from '../support/servedActionCatalog';

type WireSpec = HttpCopilotReply['proposals'][number]['spec'];
const KINDS = [
  'map.classify', 'map.extract', 'map.ner', 'map.translate', 'reduce.group_summary',
  'map.find', 'map.mcp_extract', 'research.answer', 'map.find_topic_sections',
  'derive.link_table', 'map.python', 'derive.collection_expand', 'derive.table_from_list',
  'map.ask', 'map.clean_column', 'map.clean_dates', 'map.judge', 'map.regex_extract',
  'map.summarize', 'map.template', 'map.to_geo_point', 'map.find_visual_cuts',
] as const satisfies readonly Extract<WireSpec, { action_id: string }>['action_id'][];

function proposalFor(entry: ActionCatalogEntry): CopilotProposal {
  const example = entry.examples[0] as { params?: CopilotRegisteredActionDraft['params'] } | undefined;
  if (!example?.params || entry.ui_hints.form !== 'generated') throw new Error(`Missing typed example for ${entry.kind}`);
  const params = structuredClone(example.params);
  const createsSheet = entry.ui_hints.typed_action?.creates_sheet === true;
  const fields = params.fields as Array<{ name: string }> | undefined;
  const outputKeys = fields?.map((field) => field.name) ?? entry.ui_hints.logical_outputs.map((output) => output.key);
  return { kind: entry.kind.startsWith('research.') ? 'research'
    : entry.kind.startsWith('reduce.') ? 'reduce' : createsSheet ? 'derive' : 'map',
  title: `Proposal ${entry.kind}`, spec: {
    action_id: entry.kind,
    scope: entry.row_scope_policy?.kind === 'sheet_rows'
      ? { kind: 'sheet_rows', sheet_id: 1, row_ids: [2, 4] } : { kind: 'project' },
    ...(createsSheet ? { sheet_name: 'Derived items' } : {}), params,
    output_names: createsSheet ? {} : Object.fromEntries(outputKeys.map((key) => [key, `result_${key}`])),
  } };
}

function transport(projectId: string, challenge = false) {
  const posted: Record<string, unknown>[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    expect(String(input)).toBe(`/api/projects/${projectId}/actions/v1/run`);
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    posted.push(body);
    const needsConfirmation = challenge && body.confirmation !== 'proposal-retry-hash';
    const materializes = body.sheet_name !== undefined;
    return new Response(JSON.stringify({ schema_version: 'frisket.action_result.v1',
      action: { kind: body.action_id, action_id: `proposal-${posted.length}` },
      status: needsConfirmation ? 'needs_confirmation' : 'completed',
      run_id: needsConfirmation || materializes ? null : 7000 + posted.length,
      receipt_id: needsConfirmation ? null : `receipt-${posted.length}`,
      outputs: materializes && !needsConfirmation ? [{ kind: 'sheet', sheet_id: 91,
        name: body.sheet_name, ref: { kind: 'materialized_sheet', sheet_id: 91, row_count: 2 } }] : [],
      errors: needsConfirmation ? [{ code: 'confirmation_required', message: 'Confirm this proposal run.',
        field: 'confirmation', details: { reason: 'model_metered', promise_set_hash: 'proposal-retry-hash' } }] : [],
    }), { status: needsConfirmation ? 402 : 200, headers: { 'Content-Type': 'application/json' } });
  }));
  return posted;
}

afterEach(() => vi.unstubAllGlobals());
const describeServed = hasServedActionCatalogPython() || process.env.CI ? describe : describe.skip;
const catalog = hasServedActionCatalogPython() || process.env.CI ? servedActionCatalog() : null;
describeServed('Copilot typed direct-run wire', () => {
  it('posts typed proposals with scope, nested Params, and destinations unchanged', async () => {
    const api = createProjectApi('proposal-matrix');
    const posted = transport('proposal-matrix');
    const entries = catalog!.actions.filter((entry) => KINDS.includes(entry.kind as typeof KINDS[number]));
    expect(entries.map((entry) => entry.kind).sort()).toEqual([...KINDS].sort());
    const proposals = entries.map(proposalFor);
    const before = structuredClone(proposals);
    for (const proposal of proposals) await api.runProposal(proposal);
    expect(proposals).toEqual(before);
    expect(posted).toHaveLength(proposals.length);
    for (const [index, body] of posted.entries()) {
      expect(body).toEqual({ ...proposals[index].spec,
        idempotency_key: expect.stringContaining(`copilot-${entries[index].kind}:`) });
    }
  });

  it('returns the committed child sheet for a runless materialization', async () => {
    const api = createProjectApi('proposal-sheet');
    transport('proposal-sheet');
    const entry = catalog!.actions.find((item) => item.kind === 'derive.table_from_list')!;
    await expect(api.runProposal(proposalFor(entry))).resolves.toEqual({ run_id: null, output_sheet_id: '91' });
  });

  it.each([
    { confirmed: false, hash: undefined }, { confirmed: true, hash: undefined },
    { confirmed: false, hash: 'hash-only' }, { confirmed: true, hash: 'approved-hash' },
  ])('carries authorization only for confirmed=$confirmed hash=$hash', async ({ confirmed, hash }) => {
    const api = createProjectApi('proposal-confirmation');
    const posted = transport('proposal-confirmation');
    const entry = catalog!.actions.find((item) => item.kind === 'map.classify')!;
    const proposal = proposalFor(entry);
    await api.runProposal(proposal, confirmed, hash);
    expect(posted[0]).toEqual({ ...proposal.spec, idempotency_key: expect.any(String),
      ...(confirmed && hash ? { confirmation: hash } : {}) });
  });

  it('reuses request identity and Params through the quoted confirmation retry', async () => {
    const api = createProjectApi('proposal-retry');
    const posted = transport('proposal-retry', true);
    const entry = catalog!.actions.find((item) => item.kind === 'map.classify')!;
    const proposal = proposalFor(entry);
    await expect(api.runProposal(proposal)).rejects.toThrow('Confirm this proposal run.');
    await api.runProposal(proposal, true, 'proposal-retry-hash');
    expect(posted).toHaveLength(2);
    expect(posted[1]).toEqual({ ...posted[0], confirmation: 'proposal-retry-hash' });
  });
});
