// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import type { CanonicalActionDraft } from '../../src/actions/canonicalActionDraft';
import { listProviders } from '../../src/api/open';
import { isGeneratedActionCatalogEntry, type ActionCatalogPayload } from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { aiMeta, columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { hasServedActionCatalogPython, servedActionCatalog } from '../support/servedActionCatalog';

vi.mock('../../src/api/open', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/open')>(), listProviders: vi.fn(),
}));

const SHEET = sheetMeta([
  columnDef({ id: '11', name: 'body', type: 'text' }),
  columnDef({ id: '12', name: 'answer', type: 'text', ai: aiMeta() }),
], { id: '7', name: 'Stories', rowCount: 2 });
const MODEL = 'anthropic/claude-haiku-4-5';
const TEMPLATE = { text: 'Literal text and {{body}}' };
const cases: Array<{ kind: string; field: string; params: CanonicalActionDraft }> = [
  { kind: 'map.template', field: 'template', params: { template: TEMPLATE } },
  { kind: 'research.web_search', field: 'query', params: { query: TEMPLATE, max_results: 3 } },
  { kind: 'map.ask', field: 'source', params: { source: TEMPLATE, model: MODEL,
    question: 'What changed?', context: 'Saved context' } },
  { kind: 'map.summarize', field: 'source', params: { source: TEMPLATE, model: MODEL,
    preset: 'quotes', instruction: null, context: 'Saved context' } },
  { kind: 'map.judge', field: 'source', params: { source: TEMPLATE, model: MODEL,
    judged_column: 'answer', guidelines: 'Check accuracy', include_original_prompt: false } },
];

describe.skipIf(!hasServedActionCatalogPython() && !process.env.CI)('saved typed Template values', () => {
  let catalog: ActionCatalogPayload;
  beforeAll(() => {
    catalog = servedActionCatalog();
    vi.mocked(listProviders).mockResolvedValue({ schemaVersion: 'frisket.providers.v1', tier: 'local',
      providers: [{ id: 'anthropic', label: 'Anthropic', kind: 'platform_api', configured: true,
        source: 'environment', hint: null, models: [{ id: MODEL, label: 'Test model', price: null }] }] });
  }, 30_000);
  afterEach(cleanup);

  it.each(cases)('$kind edits only object.text and preserves every saved option', async ({ kind, field, params }) => {
    const entry = catalog.actions.find((candidate) => candidate.kind === kind)!;
    if (!isGeneratedActionCatalogEntry(entry)) throw new Error(`${kind} is not generated`);
    const outputs = Object.fromEntries(entry.ui_hints.logical_outputs.map(({ key }) => [key, `saved_${key}`]));
    const execute = vi.fn();
    render(<GeneratedActionForm catalogEntry={entry}
      actionTemplate={generatedActionTemplateFromCatalogEntry(entry)!} sheet={SHEET} running={false}
      initialDraft={{ action_id: kind, scope: { kind: 'sheet_rows', sheet_id: 7 }, params,
        output_names: outputs }}
      resolveParams={async () => ({ diagnostics: {}, logical_outputs: entry.ui_hints.logical_outputs })}
      estimateAction={async () => ({ cost: 0, rows: 2, billed_cost: 0 })}
      onExecute={execute} onClose={vi.fn()} />);
    const editor = screen.getByTestId(field === 'source' ? 'text-source-template-input' : `field-${field}`);
    expect(editor).toHaveValue(TEMPLATE.text);
    const next = 'Updated literal {{body}}';
    fireEvent.change(editor, { target: { value: next } });
    const createsSheet = entry.ui_hints.typed_action?.creates_sheet === true;
    if (createsSheet) fireEvent.change(screen.getByTestId('field-sheet_name'), { target: { value: 'Search results' } });
    const run = screen.getByTestId(createsSheet ? 'run-button' : 'generated-action-run');
    await waitFor(() => expect(run).toBeEnabled());
    fireEvent.click(run);
    expect(execute).toHaveBeenCalledTimes(1);
    expect(execute.mock.calls[0][0].params).toEqual({ ...params, [field]: { text: next } });
    expect(execute.mock.calls[0][0].output_names).toEqual(outputs);
  });
});
