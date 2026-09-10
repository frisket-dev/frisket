import { beforeEach, describe, expect, it } from 'vitest';

import type { GridFilterSpec, SheetMeta } from '../../src/api/types';
import {
  INVALID_ENTITY_FILTER_VALUE,
  INVALID_IN_FILTER_VALUE,
  entityFilterValue,
  exactViewHiddenColumns,
  filterConditionFromDraft,
  gridFilterLabel,
  hiddenColumnsKey,
  loadHiddenColumns,
  normalizeGridFilterSpec,
  shownDefaultColumnsKey,
  spliceColumnOrderBeside,
} from '../../src/workspace/gridColumnState';
import { filterOperatorOptions } from '../../src/workspace/workspaceState';

const sheet: SheetMeta = {
  id: 'sheet-1',
  name: 'YouTube',
  rowCount: 1,
  citedColumnIds: [],
  columns: [
    { id: '1', name: 'title', type: 'text' },
    { id: '2', name: 'source_url', type: 'link' },
    { id: '3', name: 'url', type: 'link', defaultHidden: true },
    { id: '4', name: 'source_raw', type: 'json', defaultHidden: true },
  ],
};

describe('default-hidden grid columns', () => {
  beforeEach(() => localStorage.clear());

  it('merges host defaults with user-hidden columns without hiding schema data', () => {
    localStorage.setItem(hiddenColumnsKey('project-1', sheet.id), JSON.stringify(['title']));
    expect(loadHiddenColumns('project-1', sheet)).toEqual(['title', 'url', 'source_raw']);
  });

  it('keeps an explicitly revealed default column visible after reload', () => {
    localStorage.setItem(
      shownDefaultColumnsKey('project-1', sheet.id),
      JSON.stringify(['url']),
    );
    expect(loadHiddenColumns('project-1', sheet)).toEqual(['source_raw']);
  });
});

describe('exact Saved View columns', () => {
  it('hides schema columns added after the visible list was captured', () => {
    const grownSheet: SheetMeta = {
      ...sheet,
      columns: [...sheet.columns, { id: '5', name: 'generated_summary', type: 'text' }],
    };
    expect(exactViewHiddenColumns(grownSheet, ['title', 'source_url'])).toEqual([
      'url',
      'source_raw',
      'generated_summary',
    ]);
  });

  it('leaves column-group presentation omissions to the group controls', () => {
    expect(
      exactViewHiddenColumns(sheet, ['title'], new Set(['source_raw'])),
    ).toEqual(['source_url', 'url']);
  });
});

// ---------------------------------------------------------------------------
// entity_eq is the structured grid filter.
//
// This module was written when every filter value was a scalar or a
// two-scalar range. The normalizer's catch-all (`String(raw)`) and the chip
// label's interpolation (`${column} ${operator} ${value}`) both stringify
// whatever they are handed, so an object payload became the literal
// "[object Object]" — persisted into a saved view in the first case, printed
// on screen in the second. These are the first tests for either function.
// ---------------------------------------------------------------------------

describe('entityFilterValue — the closed entity_eq contract', () => {
  it('accepts the three legal shapes', () => {
    expect(entityFilterValue({ type: 'person' })).toEqual({ type: 'person' });
    expect(entityFilterValue({ type: 'organization', text: 'ACME Corporation' }))
      .toEqual({ type: 'organization', text: 'ACME Corporation' });
    expect(entityFilterValue({ type: 'organization', fingerprint: 'acme corp' }))
      .toEqual({ type: 'organization', fingerprint: 'acme corp' });
  });

  it('rejects payloads outside the contract', () => {
    expect(entityFilterValue({})).toBeNull();
    expect(entityFilterValue({ type: '' })).toBeNull();
    expect(entityFilterValue({ type: 7 })).toBeNull();
    // exactly zero or one of text/fingerprint
    expect(entityFilterValue({ type: 'person', text: 'a', fingerprint: 'a' })).toBeNull();
    expect(entityFilterValue({ type: 'person', text: '' })).toBeNull();
    expect(entityFilterValue({ type: 'person', fingerprint: '' })).toBeNull();
    // unknown keys are rejected, not silently dropped
    expect(entityFilterValue({ type: 'person', nickname: 'Jon' })).toBeNull();
    expect(entityFilterValue('person')).toBeNull();
    expect(entityFilterValue(['person'])).toBeNull();
    expect(entityFilterValue(null)).toBeNull();
  });
});

describe('normalizeGridFilterSpec — saved-view restoration', () => {
  it('passes a well-formed entity_eq payload through as an object', () => {
    const restored = normalizeGridFilterSpec({
      entities: { entity_eq: { type: 'organization', fingerprint: 'acme corp' } },
    });
    expect(restored).toEqual({
      entities: { entity_eq: { type: 'organization', fingerprint: 'acme corp' } },
    });
    // The regression this branch exists for.
    expect(JSON.stringify(restored)).not.toContain('[object Object]');
  });

  it('round-trips every legal entity_eq shape through JSON, byte-for-byte', () => {
    for (const value of [
      { type: 'person' },
      { type: 'organization', text: 'ACME Corporation' },
      { type: 'organization', fingerprint: 'acme corp' },
    ]) {
      const saved = JSON.parse(JSON.stringify({ entities: { entity_eq: value } })) as unknown;
      expect(normalizeGridFilterSpec(saved)).toEqual({ entities: { entity_eq: value } });
    }
  });

  it('keeps a malformed entity_eq as the invalid sentinel instead of dropping it', () => {
    // Dropping it would silently WIDEN the restored view's row set.
    for (const bad of [{ type: '' }, { type: 'person', text: 'a', fingerprint: 'b' }, 'person']) {
      expect(normalizeGridFilterSpec({ entities: { entity_eq: bad } })).toEqual({
        entities: { entity_eq: INVALID_ENTITY_FILTER_VALUE },
      });
    }
  });

  it('still normalizes the scalar, range, and bbox operators unchanged', () => {
    expect(normalizeGridFilterSpec({ title: { contains: 42 } }))
      .toEqual({ title: { contains: '42' } });
    expect(normalizeGridFilterSpec({ day: { between: { start: '2020', end: '2021' } } }))
      .toEqual({ day: { between: { start: '2020', end: '2021' } } });
    expect(
      normalizeGridFilterSpec({
        loc: { bbox: { min_lon: 1, min_lat: 2, max_lon: 3, max_lat: 4 } },
      }),
    ).toEqual({ loc: { bbox: { min_lon: 1, min_lat: 2, max_lon: 3, max_lat: 4 } } });
  });

  it('preserves multi-select facet values', () => {
    expect(normalizeGridFilterSpec({ status: { in: ['open', 'pending'] } }))
      .toEqual({ status: { in: ['open', 'pending'] } });
  });

  it('round-trips closed list-facet selectors instead of stringifying them', () => {
    for (const selectors of [
      [
        { kind: 'scalar', value: 'NYPD' },
        { kind: 'scalar', value: false },
        { kind: 'scalar', value: 7 },
      ],
      [{ kind: 'entity', type: 'organization', text: 'NYPD' }],
    ]) {
      const saved = JSON.parse(JSON.stringify({
        extracted: { list_contains_any: selectors },
      })) as unknown;
      expect(normalizeGridFilterSpec(saved)).toEqual({
        extracted: { list_contains_any: selectors },
      });
      expect(JSON.stringify(normalizeGridFilterSpec(saved))).not.toContain('[object Object]');
    }
  });

  it('keeps malformed multi-selects loudly invalid instead of widening the view', () => {
    for (const raw of [[], ['open', 3], 'open', Array.from({ length: 101 }, (_, i) => String(i))]) {
      expect(normalizeGridFilterSpec({ status: { in: raw } })).toEqual({
        status: { in: INVALID_IN_FILTER_VALUE },
      });
    }
    expect(gridFilterLabel({ status: { in: INVALID_IN_FILTER_VALUE } }))
      .toBe('status invalid multi-value filter');
  });
});

describe('gridFilterLabel — the entity_eq chip', () => {
  const label = (value: unknown, valueLabel?: string | null): string =>
    gridFilterLabel({ entities: { entity_eq: value } } as GridFilterSpec, valueLabel);

  it('names the VALUE when the payload carries one, and the KIND always', () => {
    // The type is rendered in its SINGULAR display form: `work_of_art` is not a
    // word, `norp`/`fac` are unguessable, and the NER form's own names are
    // plurals built for a checkbox list.
    expect(label({ type: 'person' })).toBe('entities · all Person mentions');
    expect(label({ type: 'work_of_art' })).toBe('entities · all Work of art mentions');
    // An exact-spelling filter knows the value that was clicked and says it.
    expect(label({ type: 'organization', text: 'ACME Corporation' }))
      .toBe('entities · “ACME Corporation” (Organization, exact spelling)');
    // A fingerprint filter's PAYLOAD does not: it holds a comparison token, and
    // the spelling lives only in the group the user clicked. When that click
    // carried the spelling here (gridViewStore's applied.filterValueLabel) the
    // chip says it, in the same quoted-spelling shape the exact-spelling chip
    // uses; the token itself is still never printed.
    expect(label({ type: 'organization', fingerprint: 'acme corp' }, 'Acme Corp.'))
      .toBe('entities · “Acme Corp.” (Organization, grouped forms)');
    // Without one — a saved view restored, a filter applied from anywhere with
    // no group in hand — it falls back to the TYPE rather than inventing a
    // spelling. Same for an empty carried label.
    expect(label({ type: 'organization', fingerprint: 'acme corp' }))
      .toBe('entities · Organization mentions (grouped forms)');
    expect(label({ type: 'organization', fingerprint: 'acme corp' }, null))
      .toBe('entities · Organization mentions (grouped forms)');
  });

  it('ignores a carried label for the filters that already know their own value', () => {
    // Only the fingerprint branch has a value it cannot name. A stale or
    // mismatched label must not overwrite a payload that speaks for itself.
    expect(label({ type: 'organization', text: 'ACME Corporation' }, 'Acme Corp.'))
      .toBe('entities · “ACME Corporation” (Organization, exact spelling)');
    expect(label({ type: 'organization' }, 'Acme Corp.'))
      .toBe('entities · all Organization mentions');
    expect(gridFilterLabel({ title: { contains: 'acme' } }, 'Acme Corp.'))
      .toBe('title contains acme');
  });

  it('never prints the fingerprint and never calls a group an entity/match/cluster', () => {
    for (const grouped of [
      label({ type: 'organization', fingerprint: 'acme corp' }),
      label({ type: 'organization', fingerprint: 'acme corp' }, 'Acme Corp.'),
    ]) {
      expect(grouped).not.toContain('acme corp');
      for (const forbidden of ['entity', 'match', 'resolved identity', 'cluster']) {
        expect(grouped.toLowerCase()).not.toContain(forbidden);
      }
    }
  });

  it('fails visibly on a malformed payload rather than rendering [object Object]', () => {
    for (const bad of [INVALID_ENTITY_FILTER_VALUE, { type: 'person', text: 'a', fingerprint: 'b' }]) {
      expect(label(bad)).toBe('entities invalid mentions filter');
    }
    expect(label({ type: 'person' })).not.toContain('[object Object]');
    expect(label(INVALID_ENTITY_FILTER_VALUE)).not.toContain('[object Object]');
  });

  it('leaves the other operators labels alone', () => {
    expect(gridFilterLabel({ title: { contains: 'acme' } })).toBe('title contains acme');
    expect(gridFilterLabel({ loc: { bbox: { min_lon: 1, min_lat: 2, max_lon: 3, max_lat: 4 } } }))
      .toBe('loc in map area');
  });

  it('labels multi-select values and every combined column', () => {
    expect(gridFilterLabel({
      status: { in: ['open', 'pending'] },
      amount: { between: { start: '10', end: '50' } },
    })).toBe('status is open or pending · amount between 10 and 50');
  });

  it('labels list containment by the member values and preserves OR semantics', () => {
    expect(gridFilterLabel({
      agencies: {
        list_contains_any: [
          { kind: 'scalar', value: 'NYPD' },
          { kind: 'scalar', value: 'FDNY' },
        ],
      },
    } as never)).toBe('agencies contains NYPD or FDNY');
  });

  it('qualifies same-spelling entity list members by type in filter chips', () => {
    expect(gridFilterLabel({
      mentions: {
        list_contains_any: [
          { kind: 'entity', type: 'organization', text: 'NYPD' },
          { kind: 'entity', type: 'person', text: 'NYPD' },
        ],
      },
    } as never)).toBe('mentions contains NYPD (Organization) or NYPD (Person)');
  });
});

describe('entity_eq stays out of the scalar filter editor', () => {
  it('is never offered as a manual operator for any column type', () => {
    for (const type of ['text', 'date', 'boolean', 'json', 'number'] as const) {
      expect(filterOperatorOptions(type).map((option) => option.value))
        .not.toContain('entity_eq');
    }
  });

  it('refuses to build an entity_eq condition out of typed text', () => {
    expect(() => filterConditionFromDraft('entity_eq', 'Acme', '', '')).toThrow(/programmatic/);
  });

  it('still builds the scalar and range conditions', () => {
    expect(filterConditionFromDraft('contains', ' acme ', '', '')).toEqual({ contains: 'acme' });
    expect(filterConditionFromDraft('between', '', ' 2020 ', ' 2021 '))
      .toEqual({ between: { start: '2020', end: '2021' } });
  });
});

describe('date-aware filter contracts', () => {
  it('offers dynamic ranges, date parts, and invalid-value inspection only for dates', () => {
    expect(filterOperatorOptions('date').map((option) => option.value)).toEqual([
      'eq',
      'neq',
      'gte',
      'lte',
      'between',
      'date_relative',
      'date_this_year',
      'date_ytd',
      'date_year',
      'date_month',
      'date_weekday',
      'date_invalid',
    ]);
    expect(filterOperatorOptions('text').map((option) => option.value))
      .not.toContain('date_relative');
  });

  it('authors and restores dynamic relative filters without freezing them to dates', () => {
    const condition = filterConditionFromDraft('date_relative', ' 90 ', 'days', '');
    expect(condition).toEqual({ date_relative: { amount: 90, unit: 'days' } });
    expect(normalizeGridFilterSpec({ published: condition })).toEqual({
      published: { date_relative: { amount: 90, unit: 'days' } },
    });
    expect(normalizeGridFilterSpec({ published: { date_relative: 'last week' } })).toEqual({
      published: { date_relative: { amount: 0, unit: 'days' } },
    });
    expect(normalizeGridFilterSpec({ published: { date_relative: { amount: 0, unit: 'days' } } }))
      .toEqual({ published: { date_relative: { amount: 0, unit: 'days' } } });
    expect(() => filterConditionFromDraft('date_relative', '0', 'days', ''))
      .toThrow(/1 to 10000/);
    expect(() => filterConditionFromDraft('date_relative', '7', 'fortnights', ''))
      .toThrow(/days, weeks, or months/);
  });

  it('authors presets and gives every temporal filter a human label', () => {
    expect(filterConditionFromDraft('date_ytd', '', '', '')).toEqual({ date_ytd: 'true' });
    expect(filterConditionFromDraft('date_invalid', '', '', ''))
      .toEqual({ date_invalid: 'true' });
    expect(gridFilterLabel({ published: { date_relative: { amount: 3, unit: 'months' } } }))
      .toBe('published in the last 3 months');
    expect(gridFilterLabel({ published: { date_month: '3' } })).toBe('published in March');
    expect(gridFilterLabel({ published: { date_weekday: '1' } })).toBe('published on Monday');
    expect(gridFilterLabel({ published: { date_invalid: 'true' } }))
      .toBe('published is not a valid date');
  });
});

describe('spliceColumnOrderBeside — insert-left/right lands beside its anchor', () => {
  const order = ['alpha', 'beta', 'gamma'];

  it('right of a middle column', () => {
    expect(spliceColumnOrderBeside(order, 'inserted', 'alpha', 'right'))
      .toEqual(['alpha', 'inserted', 'beta', 'gamma']);
  });

  it('left of a middle column', () => {
    expect(spliceColumnOrderBeside(order, 'inserted', 'beta', 'left'))
      .toEqual(['alpha', 'inserted', 'beta', 'gamma']);
  });

  it('right of the last column', () => {
    expect(spliceColumnOrderBeside(order, 'inserted', 'gamma', 'right'))
      .toEqual(['alpha', 'beta', 'gamma', 'inserted']);
  });

  it('a vanished anchor falls back to appending', () => {
    expect(spliceColumnOrderBeside(order, 'inserted', 'deleted', 'right'))
      .toEqual(['alpha', 'beta', 'gamma', 'inserted']);
  });

  it('an already-present name moves rather than duplicating', () => {
    expect(spliceColumnOrderBeside(['alpha', 'beta', 'inserted'], 'inserted', 'alpha', 'right'))
      .toEqual(['alpha', 'inserted', 'beta']);
  });

  it('does not mutate its input', () => {
    const input = ['alpha', 'beta'];
    spliceColumnOrderBeside(input, 'x', 'alpha', 'right');
    expect(input).toEqual(['alpha', 'beta']);
  });
});
