// Pure helpers behind the Mentions panel: eligibility, the coverage
// arithmetic, the honest paging summary, and the type sectioning.

import { describe, expect, it } from 'vitest';

import type { EntityMentionGroup, SheetMeta } from '../../src/api/types';
import {
  activeMentionFilter,
  appendMentionPage,
  coverageLine,
  eligibleMentionColumns,
  groupFilterValue,
  mentionFilterLabel,
  mentionTypeSections,
  mentionsZeroState,
  surfaceFilterValue,
  typeFilterValue,
  uncoveredRows,
} from '../../src/components/mentionsPanelModel';

const SHEET: SheetMeta = {
  id: '7',
  name: 'Filings',
  rowCount: 10,
  columns: [
    { id: '1', name: 'body', type: 'text' },
    // Marked: the only eligible shape.
    { id: '2', name: 'entities', type: 'json', semanticType: 'entity_mentions' },
    // A json column whose NAME says entities but which carries no marker.
    { id: '3', name: 'entities_imported', type: 'json' },
    // A marked column of the wrong physical type.
    { id: '4', name: 'entities_text', type: 'text', semanticType: 'entity_mentions' },
  ],
};

function group(overrides: Partial<EntityMentionGroup> = {}): EntityMentionGroup {
  return {
    type: 'organization',
    selector: { kind: 'fingerprint', fingerprint: 'acme corp' },
    label: 'Acme Corp.',
    rowCount: 16,
    mentionCount: 40,
    surfaceCount: 1,
    surfaces: [{ text: 'Acme Corp.', rowCount: 16, mentionCount: 40 }],
    ...overrides,
  };
}

describe('eligibility is the marker, and only the marker', () => {
  it('accepts a marked json column and nothing else', () => {
    expect(eligibleMentionColumns(SHEET).map((column) => column.id)).toEqual(['2']);
  });

  it('has nothing to browse without a sheet', () => {
    expect(eligibleMentionColumns(null)).toEqual([]);
  });
});

describe('coverageLine — completed_rows INCLUDES failures', () => {
  it('subtracts the failing subset out of the extracted count', () => {
    expect(coverageLine({ targetRows: 1000, completedRows: 997, failedRows: 3, sheetRows: 1000 }))
      .toBe('994 of 1,000 rows extracted · 3 failed');
  });

  it('drops the failure clause when nothing failed', () => {
    expect(coverageLine({ targetRows: 40, completedRows: 40, failedRows: 0, sheetRows: 40 }))
      .toBe('40 of 40 rows extracted');
  });

  it('reports an incomplete run honestly', () => {
    expect(coverageLine({ targetRows: 1000, completedRows: 400, failedRows: 0, sheetRows: 1000 }))
      .toBe('400 of 1,000 rows extracted');
  });

  it('has no line at all for a column that was never run', () => {
    // sheet_rows is a fact about the SHEET and arrives even here — it must not
    // conjure a coverage line for an extraction that never happened.
    expect(coverageLine({
      targetRows: null, completedRows: null, failedRows: null, sheetRows: 12,
    })).toBeNull();
  });
});

// The run counters are historical facts: rows added after the run leave
// target_rows where it was, and `map.ner` refuses run.backfill, so "2 of 2
// rows extracted" stays true about the run and wrong about the sheet
// permanently. sheet_rows is what makes the gap sayable.
describe('coverageLine — rows the extraction never covered', () => {
  it('names the uncovered rows and the remedy', () => {
    expect(coverageLine({ targetRows: 2, completedRows: 2, failedRows: 0, sheetRows: 4 }))
      .toBe(
        '2 of 2 rows extracted · 2 rows on this sheet not extracted — '
        + 're-run extraction to cover the whole sheet',
      );
  });

  it('reads naturally for a single uncovered row, and counts in thousands', () => {
    expect(coverageLine({ targetRows: 3, completedRows: 3, failedRows: 0, sheetRows: 4 }))
      .toContain('1 row on this sheet not extracted');
    expect(coverageLine({ targetRows: 1000, completedRows: 1000, failedRows: 0, sheetRows: 3400 }))
      .toContain('2,400 rows on this sheet not extracted');
  });

  it('keeps the failure clause alongside it', () => {
    expect(coverageLine({ targetRows: 2, completedRows: 2, failedRows: 1, sheetRows: 4 }))
      .toBe(
        '1 of 2 rows extracted · 1 failed · 2 rows on this sheet not extracted — '
        + 're-run extraction to cover the whole sheet',
      );
  });

  it('adds NOTHING when the extraction still covers the sheet', () => {
    expect(coverageLine({ targetRows: 4, completedRows: 4, failedRows: 0, sheetRows: 4 }))
      .toBe('4 of 4 rows extracted');
    // A sheet that SHRANK is not an uncovered sheet.
    expect(coverageLine({ targetRows: 4, completedRows: 4, failedRows: 0, sheetRows: 2 }))
      .toBe('4 of 4 rows extracted');
  });

  it('claims nothing when the sheet count is missing', () => {
    // A pre-sheet_rows payload: "cannot say" is not "everything is covered",
    // but it is certainly not a warning we can justify either.
    expect(uncoveredRows({ targetRows: 2, completedRows: 2, failedRows: 0, sheetRows: null }))
      .toBe(0);
    expect(coverageLine({ targetRows: 2, completedRows: 2, failedRows: 0, sheetRows: null }))
      .toBe('2 of 2 rows extracted');
  });
});

describe('mentionsZeroState — only the two empty states, never a group count', () => {
  it('has no line at all when there are groups to show', () => {
    // A non-empty result speaks through its sections; the summary element does
    // not render, and nothing counts "groups" at the user.
    expect(mentionsZeroState(3, null)).toBeNull();
    expect(mentionsZeroState(241, null)).toBeNull();
    expect(mentionsZeroState(9, 'acme')).toBeNull();
  });

  it('separates an empty extraction from an empty search', () => {
    expect(mentionsZeroState(0, null)).toBe('No mentions found in this extraction');
    expect(mentionsZeroState(0, 'acme')).toBe('No mentions match “acme”');
  });
});

describe('the three selectors and their wording', () => {
  it('emits the group, surface, and type payloads', () => {
    expect(groupFilterValue(group())).toEqual({ type: 'organization', fingerprint: 'acme corp' });
    expect(groupFilterValue(group({ selector: { kind: 'text', text: 'March 2022' }, type: 'date' })))
      .toEqual({ type: 'date', text: 'March 2022' });
    expect(surfaceFilterValue('organization', { text: 'ACME Corp', rowCount: 1, mentionCount: 1 }))
      .toEqual({ type: 'organization', text: 'ACME Corp' });
    expect(typeFilterValue('person')).toEqual({ type: 'person' });
  });

  it('names the clicked VALUE, and the kind, without calling a group an entity or a cluster', () => {
    // The whole point of the chip: it used to say the same thing
    // ("organization · grouped forms") for every organization on the sheet.
    expect(mentionFilterLabel({ type: 'organization', fingerprint: 'acme corp' }, 'ACME Corp'))
      .toBe('“ACME Corp” · Organization, grouped forms');
    expect(mentionFilterLabel({ type: 'organization', text: 'ACME Corporation' }))
      .toBe('“ACME Corporation” · Organization, exact spelling');
    expect(mentionFilterLabel({ type: 'person' })).toBe('Person · all mentions');
    // The fingerprint is a comparison token, not a spelling anyone wrote — so
    // with no group in hand the chip names the TYPE, never the token.
    const unlabelled = mentionFilterLabel({ type: 'organization', fingerprint: 'acme corp' });
    expect(unlabelled).toBe('Organization · grouped forms');
    expect(unlabelled).not.toContain('acme corp');
  });

  it('reads back only a contract-conforming applied filter', () => {
    expect(activeMentionFilter({ entities: { entity_eq: { type: 'person' } } }, 'entities'))
      .toEqual({ type: 'person' });
    // Wrong column, malformed payload, and no filter all highlight nothing.
    expect(activeMentionFilter({ entities: { entity_eq: { type: 'person' } } }, 'other')).toBeNull();
    expect(activeMentionFilter({ entities: { entity_eq: { type: '' } } }, 'entities')).toBeNull();
    expect(activeMentionFilter(null, 'entities')).toBeNull();
  });
});

describe('mentionTypeSections — the server order is the order', () => {
  it('keeps sections in first-appearance order and groups in received order', () => {
    const sections = mentionTypeSections([
      group({ label: 'Zenith', selector: { kind: 'fingerprint', fingerprint: 'zenith' } }),
      group({ label: 'Acme Corp.' }),
      group({ type: 'date', selector: { kind: 'text', text: 'March 2022' }, label: 'March 2022' }),
    ]);
    expect(sections.map((section) => section.type)).toEqual(['organization', 'date']);
    expect(sections[0].groups.map((item) => item.label)).toEqual(['Zenith', 'Acme Corp.']);
  });

  it('keeps a type that spans a page boundary as ONE section', () => {
    const sections = mentionTypeSections([
      group({ label: 'Acme Corp.' }),
      group({ type: 'date', selector: { kind: 'text', text: 'March 2022' }, label: 'March 2022' }),
      group({ label: 'Zenith', selector: { kind: 'fingerprint', fingerprint: 'zenith' } }),
    ]);
    expect(sections.map((section) => section.type)).toEqual(['organization', 'date']);
    expect(sections[0].groups).toHaveLength(2);
  });
});

describe('mentionTypeSections — each section carries its typeTotals total', () => {
  const orgGroup = (label: string) =>
    group({ label, selector: { kind: 'fingerprint', fingerprint: label.toLowerCase() } });
  const dateGroup = group({
    type: 'date', selector: { kind: 'text', text: 'March 2022' }, label: 'March 2022',
  });

  it('states the category TOTAL from typeTotals, not the loaded length', () => {
    // Top-N-per-type is on screen (2 orgs, 1 date) but each category holds more
    // behind it: the heading count is the typeTotals fact.
    const sections = mentionTypeSections(
      [orgGroup('Acme Corp.'), orgGroup('Zenith'), dateGroup],
      [
        { type: 'organization', totalGroups: 40 },
        { type: 'date', totalGroups: 5 },
      ],
    );
    expect(sections.map((section) => [section.type, section.total, section.remaining])).toEqual([
      ['organization', 40, 38],
      ['date', 5, 4],
    ]);
  });

  it('offers no "Show more" for a category already fully loaded', () => {
    const sections = mentionTypeSections(
      [orgGroup('Acme Corp.'), orgGroup('Zenith')],
      [{ type: 'organization', totalGroups: 2 }],
    );
    expect(sections[0].total).toBe(2);
    expect(sections[0].remaining).toBe(0);
  });

  it('falls back to the loaded length when typeTotals lacks the category', () => {
    // A pre-v2 payload carries no total for the type: the loaded groups are all
    // the section can honestly claim, so nothing invites paging past them.
    const sections = mentionTypeSections([orgGroup('Acme Corp.'), orgGroup('Zenith')], []);
    expect(sections[0].total).toBe(2);
    expect(sections[0].remaining).toBe(0);
  });
});

describe('appendMentionPage — paging cannot double-count', () => {
  it('appends new groups and ignores ones already loaded', () => {
    const acme = group();
    const zenith = group({ label: 'Zenith', selector: { kind: 'fingerprint', fingerprint: 'zenith' } });
    expect(appendMentionPage([acme], [zenith]).map((item) => item.label))
      .toEqual(['Acme Corp.', 'Zenith']);
    expect(appendMentionPage([acme], [acme, zenith]).map((item) => item.label))
      .toEqual(['Acme Corp.', 'Zenith']);
  });
});
