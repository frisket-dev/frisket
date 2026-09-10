// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { decodeSavedActionSpec, encodeSavedActionSpec } from '../../src/actions/savedActionSpec';
import type { GeneratedActionCatalogEntry, GeneratedActionDraft } from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';
import { completeCatalogPayload, sheetMeta } from '../support/actionFormFixtures';

afterEach(cleanup);
const keys = ['source_row_id', 'source_value', 'target_row_id', 'target_value', 'match_score', 'match_value'];
const entry = syntheticActionCatalogEntry('derive.link_table', {
  title: 'Create link table', input_schema: { type: 'object', additionalProperties: false,
    required: ['source'], properties: {
      source: { type: 'object', required: ['kind', 'receipt_id'], properties: {
        kind: { const: 'semantic_join' }, receipt_id: { type: 'string' },
      } }, include_unmatched: { type: 'boolean', default: false },
    } },
  ui_hints: { form: 'generated', category: 'convert', semantic_controls: {},
    typed_action: { creates_sheet: true }, logical_outputs: keys.map((key) => ({ key, column_type: 'text' })),
  },
}) as GeneratedActionCatalogEntry;
const catalog = completeCatalogPayload([entry]);
const template = actionTemplatesFromCatalog(catalog)
  .find((candidate) => candidate.kind === 'derive.link_table')!;

describe('typed link table consumer', () => {
  it.each(['project', 'sheet_rows'] as const)('preserves saved %s intent through validation and execution', async (kind) => {
    const draft: GeneratedActionDraft = { action_id: 'derive.link_table',
      scope: kind === 'project' ? { kind } : { kind, sheet_id: 7, row_ids: [3, 8] },
      sheet_name: 'Reviewed links', output_names: { source_value: 'Original', match_value: 'Matched' },
      params: { source: { kind: 'semantic_join', receipt_id: 'receipt-saved-join' }, include_unmatched: true },
    };
    expect(encodeSavedActionSpec(decodeSavedActionSpec(catalog, draft))).toEqual(draft);
    const onExecute = vi.fn();
    const resolveParams = vi.fn(async () => ({ diagnostics: {}, logical_outputs: entry.ui_hints.logical_outputs }));
    render(<GeneratedActionForm catalogEntry={entry} actionTemplate={template}
      sheet={sheetMeta([], { id: '7', name: 'Source' })} initialDraft={draft} running={false}
      resolveParams={resolveParams} onExecute={onExecute} onClose={() => {}} />);
    expect(screen.getByTestId('link-table-receipt')).toHaveValue('receipt-saved-join');
    fireEvent.change(screen.getByTestId('link-table-receipt'), { target: { value: 'receipt-reviewed-join' } });
    await waitFor(() => expect(screen.getByTestId('run-button')).toBeEnabled());
    fireEvent.click(screen.getByTestId('run-button'));
    expect(onExecute).toHaveBeenCalledTimes(1);
    expect(resolveParams).toHaveBeenLastCalledWith(expect.objectContaining({ scope: draft.scope }));
    for (const [request] of onExecute.mock.calls) {
      expect(request).toMatchObject({ ...draft, params: { ...draft.params,
        source: { kind: 'semantic_join', receipt_id: 'receipt-reviewed-join' } } });
      expect(request.params).not.toHaveProperty('target_sheet_name');
      expect(request.params).not.toHaveProperty('row_ids');
    }
  });
});
