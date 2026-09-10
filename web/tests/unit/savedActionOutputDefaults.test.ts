import { describe, expect, it } from 'vitest';

import { decodeSavedActionSpec, encodeSavedActionSpec, SavedActionSpecError } from '../../src/actions/savedActionSpec';
import { servedActionCatalog } from '../support/servedActionCatalog';

const catalog = servedActionCatalog();

function savedDraft(kind: string) {
  const entry = catalog.actions.find((candidate) => candidate.kind === kind)!;
  const example = entry.examples[0];
  return {
    action_id: kind,
    scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [3, 8] },
    params: example.params,
    ...(entry.ui_hints.typed_action?.creates_sheet ? { sheet_name: 'Saved results' } : {}),
  };
}

describe('saved output names are optional renames', () => {
  it.each(['map.ner', 'reduce.group_summary', 'map.find'])(
    'accepts omitted or empty maps without widening %s source scope', (kind) => {
      const draft = savedDraft(kind);
      for (const candidate of [draft, { ...draft, output_names: {} }]) {
        const before = structuredClone(candidate);
        const decoded = decodeSavedActionSpec(catalog, candidate);
        expect(encodeSavedActionSpec(decoded)).toEqual({ ...draft, output_names: {} });
        expect(candidate).toEqual(before);
      }
    },
  );

  it('preserves a partial table rename and leaves other defaults to the host', () => {
    const draft = { ...savedDraft('reduce.group_summary'), output_names: { summary: 'Highlights' } };
    expect(encodeSavedActionSpec(decodeSavedActionSpec(catalog, draft))).toEqual(draft);
  });

  it.each([
    null, [], { unknown: 'Result' }, { summary: '' }, { summary: ' ' },
    { summary: ' Result' }, { ' summary': 'Result' },
    { summary: 'Same', group: 'Same' }, { summary: 'group' },
  ])('refuses malformed explicit renames %j', (output_names) => {
    expect(() => decodeSavedActionSpec(catalog, {
      ...savedDraft('reduce.group_summary'), output_names,
    })).toThrow(SavedActionSpecError);
  });
});
