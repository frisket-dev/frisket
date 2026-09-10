// Pure view-model helpers for the Mentions panel (MentionsPanel.tsx),
// extracted so the eligibility, coverage arithmetic, grouping, and
// active-filter readback are unit-testable without a DOM. No React, no api.
//
// The panel is READ-ONLY: it browses what map.ner already wrote into a marked
// entity column and filters the grid by it. It never starts an extraction, and
// it never adapts an unmarked column at read time.

import type {
  EntityMentionGroup,
  EntityMentionSelector,
  EntityMentionSurface,
  EntityMentionTypeTotal,
  EntityMentionsCoverage,
  GridFilterEntityValue,
  GridFilterSpec,
  SheetMeta,
} from '../api/open';
import { entityTypeName } from './action-panel/nerLabelModel';
import { entityFilterValue } from '../workspace/gridColumnState';
import { countLabel } from '../format';

export type SheetColumn = SheetMeta['columns'][number];

/** The explicit marker map.ner stamps on its output column. */
export const ENTITY_MENTIONS_SEMANTIC_TYPE = 'entity_mentions';

/** The action whose drawer both empty-state CTAs open. */
export const MENTIONS_EXTRACT_ACTION_KIND = 'map.ner';

/** One-line explainer for the panel. Deliberately says "spelling", not
 *  "entity"/"match"/"cluster": the grouping is deterministic surface merging,
 *  not identity resolution. */
export const MENTIONS_BLURB =
  'Names map.ner already found in one column, grouped by spelling. Click a ' +
  'group to filter the grid to the rows it appears in.';

export const MENTIONS_EMPTY_COLUMN_BLURB =
  'This sheet has no entity column yet. Extract entities to find the people, ' +
  'organizations, and places named in a text column, then browse them here.';

/** Eligibility is the MARKER, and only the marker: a
 *  physical `json` column on the active sheet carrying
 *  `semantic_type === 'entity_mentions'`. Cells are never sampled, object keys
 *  are never inspected, and a column called "entities" is never adopted —
 *  eligibility must not silently change as data changes. Imported JSON is not
 *  auto-adopted in v1. */
export function isEntityMentionsColumn(column: SheetColumn): boolean {
  return column.type === 'json' && column.semanticType === ENTITY_MENTIONS_SEMANTIC_TYPE;
}

export function eligibleMentionColumns(sheet: SheetMeta | null): SheetColumn[] {
  return sheet ? sheet.columns.filter(isEntityMentionsColumn) : [];
}

/** Honest coverage line for the column's current run.
 *
 *  The arithmetic is the part that is easy to get wrong: the run store's
 *  `completedRows` counts every row the run PROCESSED, failures INCLUDED, and
 *  `failedRows` is the failing subset. So the number of rows that actually
 *  produced mentions is `completedRows - failedRows`, and printing
 *  `completedRows` as "extracted" would silently count the failures as
 *  successes.
 *
 *  Returns null when the column has no current run: an un-run column has no
 *  coverage, and "0 of 0 rows extracted" would be a claim about an extraction
 *  that never happened. */
export function coverageLine(coverage: EntityMentionsCoverage): string | null {
  const { targetRows, completedRows, failedRows } = coverage;
  if (targetRows === null || completedRows === null) return null;
  const failed = failedRows ?? 0;
  const extracted = Math.max(0, completedRows - failed);
  const parts = [`${extracted.toLocaleString()} of ${targetRows.toLocaleString()} rows extracted`];
  if (failed > 0) parts.push(`${failed.toLocaleString()} failed`);
  const uncovered = uncoveredRows(coverage);
  if (uncovered > 0) {
    parts.push(
      `${countLabel(uncovered, 'row')} on this sheet not extracted — re-run `
      + 'extraction to cover the whole sheet',
    );
  }
  return parts.join(' · ');
}

/** Rows the extraction never covered: the sheet's live row count minus the
 *  run's target.
 *
 *  This is the one number the run counters cannot express. `targetRows` is a
 *  historical fact about the run and does not move when rows are added
 *  afterwards, and `map.ner` refuses `run.backfill` — so once a sheet grows,
 *  "2 of 2 rows extracted" stays true about the run and false about the sheet,
 *  permanently, until a whole-sheet re-run. Zero (rather than a guess) when
 *  either number is missing: a comparison we cannot make must not become a
 *  warning we cannot justify. */
export function uncoveredRows(coverage: EntityMentionsCoverage): number {
  const { targetRows, sheetRows, scopeKind } = coverage;
  if (scopeKind === 'exact_membership') return 0;
  if (targetRows === null || sheetRows === null) return 0;
  return Math.max(0, sheetRows - targetRows);
}

/** The zero-result line, or null when there ARE groups to show.
 *
 *  The panel no longer counts groups at the user: the per-category sections and
 *  their own totals carry that, and "group" is internal vocabulary. What
 *  remains is the one thing the sections cannot say when they are empty — WHY
 *  they are empty — and the two reasons read differently: an extraction that
 *  found nothing is not a search that matched nothing. */
export function mentionsZeroState(totalGroups: number, search: string | null): string | null {
  if (totalGroups > 0) return null;
  return search ? `No mentions match “${search}”` : 'No mentions found in this extraction';
}

/** The `entity_eq` payload a group's own selector emits. A fingerprint
 *  selector filters every spelling in the group ("grouped forms"); a text
 *  selector filters that one literal spelling. */
export function groupFilterValue(group: EntityMentionGroup): GridFilterEntityValue {
  return selectorFilterValue(group.type, group.selector);
}

export function selectorFilterValue(
  type: string,
  selector: EntityMentionSelector,
): GridFilterEntityValue {
  return selector.kind === 'fingerprint'
    ? { type, fingerprint: selector.fingerprint }
    : { type, text: selector.text };
}

/** Clicking a raw spelling always filters that EXACT surface, whatever the
 *  group around it was keyed on. */
export function surfaceFilterValue(
  type: string,
  surface: EntityMentionSurface,
): GridFilterEntityValue {
  return { type, text: surface.text };
}

export function typeFilterValue(type: string): GridFilterEntityValue {
  return { type };
}

/** The mention filter currently applied to `columnName`, if any. Reads through
 *  the same closed-contract parser the saved-view normalizer uses, so a
 *  malformed payload highlights nothing rather than matching by accident. */
export function activeMentionFilter(
  filter: GridFilterSpec | null | undefined,
  columnName: string,
): GridFilterEntityValue | null {
  return entityFilterValue(filter?.[columnName]?.entity_eq);
}

/** The spelling the active filter's group is known by, from the groups the
 *  panel has loaded — the only place that spelling exists, since a fingerprint
 *  selector carries a comparison token rather than any text. Null when the
 *  filter's group is not on the loaded page (after a reload, or once the
 *  search narrowed it away): the chip then names the type alone rather than
 *  inventing a spelling. */
export function activeGroupLabel(
  groups: readonly EntityMentionGroup[],
  active: GridFilterEntityValue | null,
): string | null {
  if (active === null) return null;
  const match = groups.find((group) => sameMentionFilter(active, groupFilterValue(group)));
  return match?.label ?? null;
}

export function sameMentionFilter(
  a: GridFilterEntityValue | null,
  b: GridFilterEntityValue,
): boolean {
  return (
    a !== null
    && a.type === b.type
    && a.text === b.text
    && a.fingerprint === b.fingerprint
  );
}

/** Human wording for which of the three mention filters is applied. The same
 *  vocabulary as the toolbar chip: "grouped forms" / "exact spelling" / bare
 *  type. Never "entity", "match", or "cluster".
 *
 *  A chip describes ONE group, so the type is rendered in its SINGULAR display
 *  form ("Organization"), not the plural the form's checkboxes use.
 *
 *  `groupLabel` is the spelling the clicked group is known by. A fingerprint
 *  selector carries no spelling — the fingerprint is a comparison token nobody
 *  wrote and must never be printed — so without this the chip could only name
 *  the TYPE, and "organization · grouped forms" is the same chip for every
 *  organization on the sheet. The caller supplies it from the group it just
 *  clicked; when it cannot (the group is not on the loaded page after a
 *  reload), the chip falls back to naming the type alone rather than guessing
 *  at a spelling. */
export function mentionFilterLabel(
  value: GridFilterEntityValue,
  groupLabel?: string | null,
): string {
  const typeName = entityTypeName(value.type);
  if (value.text !== undefined) {
    return `“${value.text}” · ${typeName}, exact spelling`;
  }
  if (value.fingerprint !== undefined) {
    return groupLabel
      ? `“${groupLabel}” · ${typeName}, grouped forms`
      : `${typeName} · grouped forms`;
  }
  return `${typeName} · all mentions`;
}

export interface MentionTypeSection {
  type: string;
  groups: EntityMentionGroup[];
  /** The category's exact constrained group count, from `typeTotals` — what the
   *  section heading states. Not the loaded length: a section shows its full
   *  count while holding only a page of it. */
  total: number;
  /** Groups of this category the panel has NOT yet loaded (`total` minus the
   *  loaded length, floored at 0) — what the section's "Show more" offers. */
  remaining: number;
}

/** Split the loaded groups into canonical-type sections, each carrying its
 *  category total from `typeTotals`.
 *
 *  Order is the SERVER's, in both dimensions: sections appear in the order
 *  their type is first seen (which reproduces the server's section order) and
 *  groups keep their received order (row count desc, then
 *  label). Nothing is re-sorted here — re-sorting would quietly disagree with
 *  the paging the same order defines. Appending (rather than chunking
 *  consecutive runs) is what keeps a type that spans a page boundary as ONE
 *  section instead of two headings with the same name.
 *
 *  The total is a `typeTotals` fact, refreshed as later pages arrive, never the
 *  loaded length. Absent that fact (a pre-v2 payload), the loaded groups are
 *  all the section can honestly claim, so `remaining` is 0 and no "Show more"
 *  appears. */
export function mentionTypeSections(
  groups: EntityMentionGroup[],
  typeTotals: EntityMentionTypeTotal[] = [],
): MentionTypeSection[] {
  const totals = new Map(typeTotals.map((entry) => [entry.type, entry.totalGroups]));
  const sections: MentionTypeSection[] = [];
  const byType = new Map<string, MentionTypeSection>();
  for (const group of groups) {
    const existing = byType.get(group.type);
    if (existing) {
      existing.groups.push(group);
      continue;
    }
    const section: MentionTypeSection = {
      type: group.type, groups: [group], total: 0, remaining: 0,
    };
    byType.set(group.type, section);
    sections.push(section);
  }
  for (const section of sections) {
    const loaded = section.groups.length;
    section.total = totals.get(section.type) ?? loaded;
    section.remaining = Math.max(0, section.total - loaded);
  }
  return sections;
}

/** Stable identity for a group across pages, for React keys and test ids. */
export function groupKey(group: EntityMentionGroup): string {
  return group.selector.kind === 'fingerprint'
    ? `${group.type}:fingerprint:${group.selector.fingerprint}`
    : `${group.type}:text:${group.selector.text}`;
}

/** Merge a freshly-arrived page onto the groups already in hand, dropping any
 *  group id already loaded so a shifting sheet can never double-count a group
 *  into the "showing N of M" line. */
export function appendMentionPage(
  loaded: EntityMentionGroup[],
  page: EntityMentionGroup[],
): EntityMentionGroup[] {
  const seen = new Set(loaded.map(groupKey));
  return [...loaded, ...page.filter((group) => !seen.has(groupKey(group)))];
}

// The three count labels below are format.ts's countLabel with the panel's
// nouns filled in — hand-rolling `n === 1 ? 'row' : 'rows'` three times next
// door to the shared helper is how a pluralization bug gets fixed in one place
// and stays live in another.
export function rowCountLabel(rowCount: number): string {
  return countLabel(rowCount, 'row');
}

export function mentionCountLabel(mentionCount: number): string {
  return countLabel(mentionCount, 'mention');
}

/** Disclosure hint on a multi-spelling group. "forms" (never "variants of the
 *  same entity") because the group is spellings, not identities. */
export function formsLabel(surfaceCount: number): string {
  return countLabel(surfaceCount, 'form');
}
