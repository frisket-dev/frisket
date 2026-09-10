import { describe, expect, it } from 'vitest';
import { decodeSavedActionSpec, encodeSavedActionSpec } from '../../src/actions/savedActionSpec';
import { canonicalActionDraftFromValidatedParams, serializeCanonicalActionDraft } from '../../src/actions/canonicalActionDraft';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { servedActionCatalog } from '../support/servedActionCatalog';

const catalog = servedActionCatalog();
const entry = catalog.actions.find((item) => item.kind === 'cluster.values')!;
const review = { source_hash: `sha256:${'a'.repeat(64)}`,
  canonical_overrides: { 'jon smith': 'Jonathan Smith' },
  excluded_members: { 'jon smith': ['Smith, Jon'] } };
function saved(params: Record<string, unknown> = {}) {
  return { action_id: 'cluster.values', scope: { kind: 'sheet_rows', sheet_id: 7 },
    params: { source: 'name', method: 'fingerprint', min_size: 2, review, ...params },
    output_names: { canonical: 'name_canonical' } };
}

describe('cluster canonical saved contract', () => {
  it('uses one canonical launcher identity without a legacy alias', () => {
    expect(generatedActionTemplateFromCatalogEntry(entry)?.kind).toBe('cluster.values');
  });
  it('preserves served whole-column scope, source constraints and metered declaration', () => {
    expect(entry.ui_hints.form).toBe('generated');
    expect(entry.row_scope_policy).toEqual({ kind: 'sheet_rows', selectors: ['all_rows'] });
    expect(entry.cost_policy).toMatchObject({ kind: 'model_metered', requires_confirmation: true });
    expect(entry.ui_hints.source_requirements).toContainEqual(expect.objectContaining({
      param: 'source', min: 1, accepted_column_types: ['text', 'category', 'link'],
    }));
  });
  it.each([
    { method: 'fingerprint' },
    { method: 'semantic', threshold: 0.9 },
    { method: 'ngram_fingerprint', ngram_size: 3 },
  ])('round-trips $method, nested review and independent naming', (knobs) => {
    const wire = saved({ ...knobs, key_template: '{{value|lower}}' });
    const before = structuredClone(wire);
    const decoded = decodeSavedActionSpec(catalog, wire);
    expect(encodeSavedActionSpec(decoded)).toEqual(wire);
    expect(wire).toEqual(before);
    expect(decoded.registeredDraft?.scope).not.toHaveProperty('row_ids');
    expect(decoded.params).not.toHaveProperty('sheet_id');
    expect(decoded.params).not.toHaveProperty('output_name');
    const draft = canonicalActionDraftFromValidatedParams(decoded.entry, decoded.params);
    expect(serializeCanonicalActionDraft(draft)).toEqual(wire.params);
    expect(draft.review).not.toBe(decoded.params.review);
  });
  it('changes only the destination when a reviewed draft is renamed', () => {
    const decoded = decodeSavedActionSpec(catalog, saved());
    const wire = { ...decoded.registeredDraft!, output_names: { canonical: 'Reviewed names' } };
    const reopened = decodeSavedActionSpec(catalog, wire);
    expect(reopened.params.review).toEqual(review);
    expect(encodeSavedActionSpec(reopened).output_names).toEqual({ canonical: 'Reviewed names' });
  });
  it('permits an unreviewed API draft without fabricating a review hash', () => {
    const wire = saved({ review: null });
    expect(encodeSavedActionSpec(decodeSavedActionSpec(catalog, wire))).toEqual(wire);
  });
  it.each([
    { ...review, canonical_overrides: [] },
    { ...review, excluded_members: [] },
    { ...review, excluded_members: { 'jon smith': 'Smith, Jon' } },
  ])('rejects malformed structured review without silently discarding it', (bad) => {
    expect(() => decodeSavedActionSpec(catalog, saved({ review: bad }))).toThrow();
  });
  it('fails closed when the served schema no longer accepts the saved review field', () => {
    const mutated = structuredClone(catalog);
    const changed = mutated.actions.find((item) => item.kind === entry.kind)!;
    delete changed.input_schema.properties?.review;
    expect(() => decodeSavedActionSpec(mutated, saved())).toThrow();
  });
});
