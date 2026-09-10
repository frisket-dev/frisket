import { describe, expect, it } from 'vitest';

import {
  canonicalActionDraftFromValidatedParams,
  serializeCanonicalActionDraft,
} from '../actions/canonicalActionDraft';
import {
  decodeSavedActionSpec,
  encodeSavedActionSpec,
  SavedActionSpecError,
} from '../actions/savedActionSpec';
import {
  hasServedActionCatalogPython,
  servedActionCatalog,
} from '../../tests/support/servedActionCatalog';

const describeServedCatalog = hasServedActionCatalogPython() || process.env.CI
  ? describe : describe.skip;

// Catalog generation is fixture setup, outside an individual UI assertion's timeout.
if (hasServedActionCatalogPython() || process.env.CI) servedActionCatalog();

const PARAMS = {
  right: { sheet_id: 22 },
  join_keys: [
    { left_column: 'Customer ID', right_column: 'Customer ID' },
    { left_column: 'Region', right_column: 'Region code' },
  ],
  columns: [
    { side: 'right', column: 'Customer name' },
    { side: 'left', column: 'Order total' },
    { side: 'right', column: 'Customer name' },
    { side: 'left', column: 'Region' },
  ],
  how: 'left',
  indicator: true,
  max_output_rows: 100_000,
};

function saved() {
  return {
    action_id: 'derive.join',
    scope: { kind: 'sheet_rows', sheet_id: 11, row_ids: [101, 108] },
    params: structuredClone(PARAMS),
    sheet_name: 'Joined output',
    output_names: {
      'Customer ID': 'Customer key',
      'Customer name': 'Customer',
      'Order total': 'Amount',
      'Customer name_2': 'Customer repeated',
      Region: 'Region key',
      Region_left: 'Order region',
      _merge: 'Origin',
    },
  };
}

describeServedCatalog('ordinary join canonical saved state', () => {
  it('round-trips selected primary rows, the whole right sheet, and independent output names', () => {
    const wire = saved();
    const before = structuredClone(wire);
    const decoded = decodeSavedActionSpec(servedActionCatalog(), wire);

    expect(decoded.registeredDraft).toEqual(wire);
    expect(encodeSavedActionSpec(decoded)).toEqual(wire);
    expect(wire).toEqual(before);
    expect(decoded.params.right).toEqual({ sheet_id: 22 });
    expect(decoded.params).not.toHaveProperty('left_row_ids');
    expect(decoded.params).not.toHaveProperty('right_row_ids');
  });

  it('retains all-primary scope without replacing it with a current selection', () => {
    const wire = { ...saved(), scope: { kind: 'sheet_rows', sheet_id: 11 } };
    const decoded = decodeSavedActionSpec(servedActionCatalog(), wire);
    expect(encodeSavedActionSpec(decoded)).toEqual(wire);
    expect(decoded.registeredDraft?.scope).not.toHaveProperty('row_ids');
  });

  it('hydrates ordered repeated projections as structured Params, never CSV or side buckets', () => {
    const catalog = servedActionCatalog();
    const decoded = decodeSavedActionSpec(catalog, saved());
    const draft = canonicalActionDraftFromValidatedParams(decoded.entry, decoded.params);
    expect(serializeCanonicalActionDraft(draft)).toEqual(PARAMS);
    expect(draft.columns).toEqual(PARAMS.columns);
    expect(draft.columns).not.toBe(decoded.params.columns);
    expect(draft.join_keys).not.toBe(decoded.params.join_keys);
    expect(draft).not.toHaveProperty('join_left_columns');
    expect(draft).not.toHaveProperty('target_sheet_name');
  });

  it.each(['inner', 'left', 'right', 'outer'])('round-trips %s join mode without rewriting projection order', (how) => {
    const wire = saved();
    wire.params.how = how;
    expect(encodeSavedActionSpec(decodeSavedActionSpec(servedActionCatalog(), wire))).toEqual(wire);
  });

  it('distinguishes default projection from an invalid empty explicit projection', () => {
    const catalog = servedActionCatalog();
    const all = { ...saved(), params: { ...PARAMS, columns: null } };
    expect(encodeSavedActionSpec(decodeSavedActionSpec(catalog, all))).toEqual(all);
    expect(() => decodeSavedActionSpec(catalog, {
      ...all, params: { ...PARAMS, columns: [] },
    })).toThrow(SavedActionSpecError);
  });

  it.each(['confirmation', 'idempotency_key'])('does not persist invocation authority %s', (field) => {
    expect(() => decodeSavedActionSpec(servedActionCatalog(), {
      ...saved(), [field]: 'old-invocation-authority',
    })).toThrow(SavedActionSpecError);
  });

  it.each(['left_sheet_id', 'right_sheet_id', 'left_row_ids', 'right_row_ids',
    'left_suffix', 'right_suffix', 'indicator_name', 'target_sheet_name', 'confirmed'])(
    'refuses the retired Params authority %s', (field) => {
      expect(() => decodeSavedActionSpec(servedActionCatalog(), {
        ...saved(), params: { ...PARAMS, [field]: 'legacy-value' },
      })).toThrow(SavedActionSpecError);
    },
  );

  it('refuses the retired saved envelope and launcher alias', () => {
    const catalog = servedActionCatalog();
    expect(() => decodeSavedActionSpec(catalog, {
      action_kind: 'derive.join', authoring_contract_version: 1, params: PARAMS,
    })).toThrow(SavedActionSpecError);
    expect(() => decodeSavedActionSpec(catalog, {
      ...saved(), action_id: 'derive_join',
    })).toThrow(SavedActionSpecError);
  });
});

describeServedCatalog('semantic join canonical saved state', () => {
  const semantic = {
    action_id: 'join.semantic',
    scope: { kind: 'sheet_rows', sheet_id: 11, row_ids: [101, 108] },
    params: { source: 'Body', target: { sheet_id: 22, column: 'Name' },
      carry: ['Context', 'Amount'], match_threshold: 0.72, confident_threshold: 0.91 },
    sheet_name: 'Matched records',
    output_names: { source: 'Original body', 'carry.Context': 'Context copy',
      'carry.Amount': 'Amount copy', match_value: 'Matched name', match_score: 'Similarity',
      matched_row_id: 'Matched customer ID' },
  };

  it('retains the qualified target, ordered carry columns, thresholds and coupled output names', () => {
    const decoded = decodeSavedActionSpec(servedActionCatalog(), semantic);
    const draft = canonicalActionDraftFromValidatedParams(decoded.entry, decoded.params);
    expect(decoded.registeredDraft).toEqual(semantic);
    expect(encodeSavedActionSpec(decoded)).toEqual(semantic);
    expect(serializeCanonicalActionDraft(draft)).toEqual(semantic.params);
    expect(draft.carry).not.toBe(semantic.params.carry);
    expect(draft.target).not.toBe(semantic.params.target);
  });

  it.each([
    { source: 'Other body' },
    { target: { sheet_id: 22, column: 'Other name' } },
    { target: { sheet_id: 33, column: 'Name' } },
    { carry: ['Amount', 'Context'] },
    { carry: ['Context'] },
    { match_threshold: 0.75 },
    { confident_threshold: 0.95 },
  ])('retains changed semantic intent %j instead of a legacy request projection', (change) => {
    const wire = { ...semantic, params: { ...semantic.params, ...change } };
    const decoded = decodeSavedActionSpec(servedActionCatalog(), wire);
    expect(encodeSavedActionSpec(decoded)).toEqual(wire);
    expect(decoded.params).not.toEqual(semantic.params);
  });

  it('keeps all-primary scope distinct from a selected-row saved intent', () => {
    const wire = { ...semantic, scope: { kind: 'sheet_rows', sheet_id: 11 } };
    expect(encodeSavedActionSpec(decodeSavedActionSpec(servedActionCatalog(), wire))).toEqual(wire);
  });

  it.each(['sheet_id', 'input_columns', 'target_sheet', 'target_column', 'carry_columns',
    'child_sheet', 'output_name', 'confirmed', 'consented_promise_set_hash'])(
    'refuses retired semantic Params field %s', (field) => {
      expect(() => decodeSavedActionSpec(servedActionCatalog(), {
        ...semantic, params: { ...semantic.params, [field]: 'old-value' },
      })).toThrow(SavedActionSpecError);
    },
  );

  it('refuses invocation authority, a retired launcher alias, and the legacy saved envelope', () => {
    const catalog = servedActionCatalog();
    for (const invalid of [
      { ...semantic, confirmation: 'old-consent' },
      { ...semantic, idempotency_key: 'old-run' },
      { ...semantic, action_id: 'semantic_join' },
      { action_kind: 'join.semantic', authoring_contract_version: 1, params: semantic.params },
    ]) expect(() => decodeSavedActionSpec(catalog, invalid)).toThrow(SavedActionSpecError);
  });
});
