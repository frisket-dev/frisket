// @vitest-environment jsdom
//
// The Mentions panel browses fingerprint-grouped named-entity mentions that
// map.ner ALREADY wrote into a marked entity column, and filters the grid by them.
//
// Pins the five panel states, and the four things that are easy to get
// silently wrong:
//   * eligibility is the marker only — a legacy/unmarked json column is never
//     read, so its old items can never appear as a "fallback";
//   * which selector each click emits (grouped forms vs exact spelling vs
//     type-only) — the three filter DIFFERENT row sets;
//   * single-select — a second mention filter REPLACES the first;
//   * the coverage arithmetic — completed_rows INCLUDES failures.

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      entityMentionsPreview: vi.fn(),
    },
  };
});

import { MentionsPanel } from '../../src/components/MentionsPanel';
import { ApiError } from '../../src/api/open';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('test-project');
import type {
  EntityMentionGroup,
  EntityMentionsPreview,
  SheetMeta,
} from '../../src/api/open';

const entityMentionsPreview = vi.spyOn(api, 'entityMentionsPreview');
const mockApi = { entityMentionsPreview };
const { render } = createWorkspaceTestHarness({
  projectId: 'test-project', api: { projectApi: api },
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const MARKED_SHEET: SheetMeta = {
  id: '7',
  name: 'Filings',
  rowCount: 1000,
  columns: [
    { id: '11', name: 'body', type: 'text' },
    { id: '12', name: 'entities', type: 'json', semanticType: 'entity_mentions' },
  ],
};

const TWO_MARKED_SHEET: SheetMeta = {
  ...MARKED_SHEET,
  columns: [
    ...MARKED_SHEET.columns,
    { id: '13', name: 'entities_llm', type: 'json', semanticType: 'entity_mentions' },
  ],
};

const UNRELATED_SHEET: SheetMeta = {
  id: '7',
  name: 'Filings',
  rowCount: 1000,
  columns: [
    { id: '11', name: 'body', type: 'text' },
    { id: '21', name: 'ocr_blocks', type: 'json', currentRunId: '901' },
  ],
};

const ACME: EntityMentionGroup = {
  type: 'organization',
  selector: { kind: 'fingerprint', fingerprint: 'acme corp' },
  label: 'Acme Corp.',
  rowCount: 16,
  mentionCount: 40,
  surfaceCount: 3,
  surfaces: [
    { text: 'Acme Corp.', rowCount: 12, mentionCount: 31 },
    { text: 'ACME Corporation', rowCount: 3, mentionCount: 6 },
    { text: 'Acme corp', rowCount: 1, mentionCount: 3 },
  ],
};

const ZENITH: EntityMentionGroup = {
  type: 'organization',
  selector: { kind: 'fingerprint', fingerprint: 'zenith' },
  label: 'Zenith',
  rowCount: 4,
  mentionCount: 4,
  surfaceCount: 1,
  surfaces: [{ text: 'Zenith', rowCount: 4, mentionCount: 4 }],
};

const GLOBEX: EntityMentionGroup = {
  type: 'organization',
  selector: { kind: 'fingerprint', fingerprint: 'globex' },
  label: 'Globex',
  rowCount: 3,
  mentionCount: 3,
  surfaceCount: 1,
  surfaces: [{ text: 'Globex', rowCount: 3, mentionCount: 3 }],
};

const MARCH: EntityMentionGroup = {
  type: 'date',
  selector: { kind: 'text', text: 'March 2022' },
  label: 'March 2022',
  rowCount: 2,
  mentionCount: 2,
  surfaceCount: 1,
  surfaces: [{ text: 'March 2022', rowCount: 2, mentionCount: 2 }],
};

function preview(overrides: Partial<EntityMentionsPreview> = {}): EntityMentionsPreview {
  return {
    sheetId: '7',
    column: { id: '12', name: 'entities', semanticType: 'entity_mentions' },
    coverage: { targetRows: 1000, completedRows: 997, failedRows: 3, sheetRows: 1000 },
    search: null,
    type: null,
    totalGroups: 3,
    // The v2 per-category totals: by default the loaded page IS the whole
    // extraction (2 organizations, 1 date), so no section offers "Show more".
    typeTotals: [
      { type: 'organization', totalGroups: 2 },
      { type: 'date', totalGroups: 1 },
    ],
    limit: 25,
    offset: 0,
    items: [ACME, ZENITH, MARCH],
    ...overrides,
  };
}

function renderPanel(props: Partial<Parameters<typeof MentionsPanel>[0]> = {}) {
  return render(
    <MentionsPanel
      sheets={[MARKED_SHEET]}
      activeSheetId="7"
      gridFilter={null}
      {...props}
    />,
  );
}

// ---------------------------------------------------------------------------
// State 1 + 2: nothing to browse.
// ---------------------------------------------------------------------------

describe('MentionsPanel — no marked column', () => {
  it('explains itself and offers Extract entities, which OPENS the drawer', async () => {
    const onExtract = vi.fn();
    renderPanel({ sheets: [UNRELATED_SHEET], onExtractEntities: onExtract });

    const cta = await screen.findByTestId('mentions-extract-cta');
    expect(cta).toHaveTextContent('Extract entities');
    fireEvent.click(cta);
    expect(onExtract).toHaveBeenCalledTimes(1);
    // Opening a drawer is not running an extraction, and an unmarked json
    // column is never read.
    expect(mockApi.entityMentionsPreview).not.toHaveBeenCalled();
  });

  it('never adopts an unmarked json column, whatever it is called', async () => {
    renderPanel({
      sheets: [{
        ...MARKED_SHEET,
        columns: [{ id: '30', name: 'entities', type: 'json' }],
      }],
      onExtractEntities: vi.fn(),
    });
    expect(await screen.findByTestId('mentions-extract-cta')).toBeInTheDocument();
    expect(mockApi.entityMentionsPreview).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// State 3 + 4: zero groups, loading, error.
// ---------------------------------------------------------------------------

describe('MentionsPanel — zero groups, loading, and errors', () => {
  it('says no mentions were found and still shows coverage', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(
      preview({ totalGroups: 0, items: [], coverage: { targetRows: 40, completedRows: 40, failedRows: 0, sheetRows: 40 } }),
    );
    const onExtract = vi.fn();
    renderPanel({ onExtractEntities: onExtract });

    await waitFor(() =>
      expect(screen.getByTestId('mentions-summary')).toHaveTextContent(
        'No mentions found in this extraction',
      ));
    expect(screen.getByTestId('mentions-coverage')).toHaveTextContent('40 of 40 rows extracted');
    // A way OUT of the empty state: change the settings and re-run — which
    // opens the drawer rather than launching an extraction.
    fireEvent.click(screen.getByTestId('mentions-rerun-link'));
    expect(onExtract).toHaveBeenCalledTimes(1);
    // The note is the way OUT of the empty state, not a second sentence
    // repeating the summary line directly above it.
    expect(screen.getByTestId('mentions-zero-groups-note'))
      .not.toHaveTextContent('Nothing was found in this extraction');
  });

  it('uses the standard loading treatment before the first page lands', () => {
    mockApi.entityMentionsPreview.mockReturnValue(new Promise(() => {}));
    renderPanel();
    expect(screen.getByTestId('mentions-loading')).toBeInTheDocument();
  });

  it('surfaces a typed ApiError in the standard error treatment', async () => {
    mockApi.entityMentionsPreview.mockRejectedValue(
      new ApiError(400, 'that column is not an entity column', 'column_not_entity_mentions'),
    );
    renderPanel();

    const error = await screen.findByTestId('mentions-error');
    expect(error).toHaveTextContent('that column is not an entity column');
    expect(error).toHaveAttribute('role', 'alert');
  });
});

// ---------------------------------------------------------------------------
// State 5: results.
// ---------------------------------------------------------------------------

describe('MentionsPanel — results', () => {
  it('hides the picker for one eligible column and shows it for several', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(preview());
    const single = renderPanel();
    await screen.findByTestId('mentions-type-organization');
    expect(screen.queryByTestId('mentions-column-select')).not.toBeInTheDocument();
    single.unmount();

    renderPanel({ sheets: [TWO_MARKED_SHEET] });
    expect(await screen.findByTestId('mentions-column-select')).toBeInTheDocument();
  });

  it('renders a section for EVERY category at once, with distinct rows primary', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(preview());
    renderPanel();

    // Every category the base page carried is on screen together — no global
    // "load more" stands between organizations and the date section.
    const org = await screen.findByTestId('mentions-type-organization');
    expect(org).toBeInTheDocument();
    expect(screen.getByTestId('mentions-type-date')).toBeInTheDocument();
    // The base load is ONE untyped request for the top-N of each category.
    expect(mockApi.entityMentionsPreview).toHaveBeenCalledTimes(1);
    expect(mockApi.entityMentionsPreview).toHaveBeenCalledWith(
      expect.objectContaining({ limit: 25, offset: 0 }),
    );
    expect(mockApi.entityMentionsPreview).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: expect.anything() }),
    );

    const acme = screen.getByTestId('mentions-group-organization-fingerprint-acme-corp');
    expect(acme).toHaveTextContent('Acme Corp.');
    expect(acme).toHaveTextContent('16 rows');
    expect(acme).toHaveTextContent('40 mentions');
    // A multi-spelling group discloses HOW MANY spellings it merged.
    expect(acme).toHaveTextContent('3 forms');
  });

  it('heads each section with its typeTotals count and never the word "groups"', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(
      // 42 organizations exist; the base page holds 2 of them, and the heading
      // states the total, not the loaded length.
      preview({
        items: [ACME, ZENITH, MARCH],
        totalGroups: 43,
        typeTotals: [
          { type: 'organization', totalGroups: 42 },
          { type: 'date', totalGroups: 1 },
        ],
      }),
    );
    renderPanel();

    const orgToggle = await screen.findByTestId('mentions-type-toggle-organization');
    expect(orgToggle).toHaveTextContent('42');
    expect(screen.getByTestId('mentions-type-toggle-date')).toHaveTextContent('1');
    // No COUNT reads in "group"/"groups": the old "Showing N of M groups" and
    // "N groups" wording is gone. ("grouped forms" in the blurb and the filter
    // labels is the feature's verb, not a count, and stays.)
    const panel = screen.getByTestId('mentions-panel');
    expect(panel).not.toHaveTextContent(/\d[\d,]*\s+groups?\b/i);
    expect(panel).not.toHaveTextContent('Showing');
    // The zero-state summary element only exists when there is nothing to show.
    expect(screen.queryByTestId('mentions-summary')).not.toBeInTheDocument();
  });

  it('renders a single-spelling group as a clean row with no forms hint or caret', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(preview());
    renderPanel();

    const zenith = await screen.findByTestId('mentions-group-organization-fingerprint-zenith');
    expect(zenith).toHaveTextContent('4 rows');
    expect(zenith).not.toHaveTextContent('forms');
    expect(
      screen.queryByTestId('mentions-group-expand-organization-fingerprint-zenith'),
    ).not.toBeInTheDocument();
  });

  it('searches on the SERVER rather than filtering the loaded page', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(preview());
    renderPanel();
    await screen.findByTestId('mentions-type-organization');

    fireEvent.change(screen.getByTestId('mentions-search'), { target: { value: 'acme' } });
    await waitFor(() =>
      expect(mockApi.entityMentionsPreview).toHaveBeenCalledWith(
        expect.objectContaining({ search: 'acme' }),
      ));
  });

  it('renders the received order, never a re-sort of its own', async () => {
    // Deliberately NOT in row-count order: the server sorts sections by total
    // coverage first, and the panel must not second-guess it.
    mockApi.entityMentionsPreview.mockResolvedValue(
      preview({ items: [MARCH, ACME, ZENITH], totalGroups: 3 }),
    );
    renderPanel();

    await screen.findByTestId('mentions-type-date');
    const sections = screen.getAllByTestId(/^mentions-type-(?!toggle|filter)/);
    expect(sections.map((node) => node.getAttribute('data-testid'))).toEqual([
      'mentions-type-date',
      'mentions-type-organization',
    ]);
  });
});

// ---------------------------------------------------------------------------
// Disclosure + selector emission.
// ---------------------------------------------------------------------------

describe('MentionsPanel — disclosure and the three selectors', () => {
  it('expands raw spellings from an INDEPENDENT caret, without filtering', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(preview());
    const onFilter = vi.fn();
    renderPanel({ onFilterEntity: onFilter });

    const caret = await screen.findByTestId('mentions-group-expand-organization-fingerprint-acme-corp');
    expect(screen.queryByTestId('mentions-surfaces-organization-fingerprint-acme-corp'))
      .not.toBeInTheDocument();

    fireEvent.click(caret);
    const surfaces = screen.getByTestId('mentions-surfaces-organization-fingerprint-acme-corp');
    expect(surfaces).toHaveTextContent('ACME Corporation');
    expect(surfaces).toHaveTextContent('12 rows');
    // Disclosing is not filtering.
    expect(onFilter).not.toHaveBeenCalled();

    fireEvent.click(caret);
    expect(screen.queryByTestId('mentions-surfaces-organization-fingerprint-acme-corp'))
      .not.toBeInTheDocument();
  });

  it('emits {type, fingerprint} for a combined group', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(preview());
    const onFilter = vi.fn();
    renderPanel({ onFilterEntity: onFilter });

    fireEvent.click(await screen.findByTestId('mentions-group-organization-fingerprint-acme-corp'));
    // Third argument: the group's SPELLING, carried up beside the selector.
    // The fingerprint is a comparison token nobody wrote, so this click is the
    // only moment the toolbar chip can learn what was clicked — without it the
    // chip can name the type alone, which is the same chip for every
    // organization on the sheet.
    expect(onFilter).toHaveBeenCalledWith(
      '12',
      { type: 'organization', fingerprint: 'acme corp' },
      'Acme Corp.',
    );
  });

  it('emits {type, text} for a raw spelling, labelled exact spelling', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(preview());
    const onFilter = vi.fn();
    renderPanel({ onFilterEntity: onFilter });

    fireEvent.click(await screen.findByTestId('mentions-group-expand-organization-fingerprint-acme-corp'));
    const surface = screen.getByTestId('mentions-surface-ACME-Corporation');
    expect(surface).toHaveTextContent('exact spelling');
    fireEvent.click(surface);
    // No spelling rides along: an exact-spelling payload already carries its
    // own text, and the chip prints that rather than a carried label.
    expect(onFilter).toHaveBeenCalledWith(
      '12',
      { type: 'organization', text: 'ACME Corporation' },
      undefined,
    );
  });

  it('emits {type} from the heading’s SEPARATE filter affordance; the heading only collapses', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(preview());
    const onFilter = vi.fn();
    renderPanel({ onFilterEntity: onFilter });

    const heading = await screen.findByTestId('mentions-type-toggle-organization');
    fireEvent.click(heading);
    expect(onFilter).not.toHaveBeenCalled();
    expect(screen.queryByTestId('mentions-groups-organization')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('mentions-type-filter-organization'));
    // A type filter names no single value, so it carries no spelling either.
    expect(onFilter).toHaveBeenCalledWith('12', { type: 'organization' }, undefined);
  });

  it('is single-select: a second mention filter REPLACES the first', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(preview());
    const onFilter = vi.fn();
    const view = render(
      <MentionsPanel
        sheets={[MARKED_SHEET]}
        activeSheetId="7"
        gridFilter={{ entities: { entity_eq: { type: 'organization', fingerprint: 'acme corp' } } }}
        onFilterEntity={onFilter}
      />,
    );

    // The applied one reads back as active…
    const acme = await screen.findByTestId('mentions-group-organization-fingerprint-acme-corp');
    expect(acme).toHaveAttribute('aria-pressed', 'true');
    // The chip names the SPELLING the clicked group is known by, resolved from
    // the loaded groups — the filter payload holds only a fingerprint, so
    // without that lookup every organization filter produced the same chip.
    expect(screen.getByTestId('mentions-active-filter'))
      .toHaveTextContent('“Acme Corp.” · Organization, grouped forms');

    // …and applying another emits a whole new single-column payload, which the
    // host writes as a REPLACEMENT rather than an added condition.
    fireEvent.click(screen.getByTestId('mentions-group-organization-fingerprint-zenith'));
    expect(onFilter).toHaveBeenCalledTimes(1);
    expect(onFilter).toHaveBeenCalledWith(
      '12',
      { type: 'organization', fingerprint: 'zenith' },
      'Zenith',
    );

    view.rerender(
      <MentionsPanel
        sheets={[MARKED_SHEET]}
        activeSheetId="7"
        gridFilter={{ entities: { entity_eq: { type: 'organization', fingerprint: 'zenith' } } }}
        onFilterEntity={onFilter}
      />,
    );
    expect(screen.getByTestId('mentions-group-organization-fingerprint-acme-corp'))
      .toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByTestId('mentions-group-organization-fingerprint-zenith'))
      .toHaveAttribute('aria-pressed', 'true');
  });

  it('offers a removable chip naming the VALUE and the kind of filter applied', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(preview());
    const onClear = vi.fn();
    render(
      <MentionsPanel
        sheets={[MARKED_SHEET]}
        activeSheetId="7"
        gridFilter={{ entities: { entity_eq: { type: 'organization', text: 'ACME Corporation' } } }}
        onFilterEntity={vi.fn()}
        onClearFilter={onClear}
      />,
    );

    const chip = await screen.findByTestId('mentions-active-filter');
    expect(chip).toHaveTextContent('“ACME Corporation” · Organization, exact spelling');
    fireEvent.click(screen.getByTestId('mentions-active-filter-clear'));
    expect(onClear).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// Honest paging + coverage.
// ---------------------------------------------------------------------------

describe('MentionsPanel — per-section paging and coverage', () => {
  // 42 organizations, of which the base page carries 2; the second page of
  // organizations answers this category's own "Show more".
  const MANY_ORGS = preview({
    items: [ACME, ZENITH, MARCH],
    totalGroups: 43,
    typeTotals: [
      { type: 'organization', totalGroups: 42 },
      { type: 'date', totalGroups: 1 },
    ],
  });
  const ORG_PAGE_TWO = preview({
    type: 'organization',
    items: [GLOBEX],
    totalGroups: 42,
    typeTotals: [{ type: 'organization', totalGroups: 42 }],
    offset: 2,
    limit: 100,
  });

  it('offers a Show more only in categories that have more behind them', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(MANY_ORGS);
    renderPanel();

    // Organizations has 42 but shows 2, so its section offers the next page…
    const more = await screen.findByTestId('mentions-show-more-organization');
    expect(more).toHaveTextContent('Show 40 more');
    expect(more).toHaveAccessibleName('Show 40 more Organizations');
    // …while the fully-loaded date section offers nothing.
    expect(screen.queryByTestId('mentions-show-more-date')).not.toBeInTheDocument();
  });

  it('appends a category page into ONLY that section and refreshes its total', async () => {
    mockApi.entityMentionsPreview
      .mockResolvedValueOnce(MANY_ORGS)
      .mockResolvedValueOnce(ORG_PAGE_TWO);
    renderPanel();

    await screen.findByTestId('mentions-show-more-organization');
    expect(within(screen.getByTestId('mentions-groups-organization')).getAllByTestId('mentions-group-row'))
      .toHaveLength(2);
    expect(screen.queryByTestId('mentions-group-organization-fingerprint-globex'))
      .not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('mentions-show-more-organization'));

    // The typed page is a request for THIS category, paged past the 2 already
    // in hand.
    await waitFor(() =>
      expect(screen.getByTestId('mentions-group-organization-fingerprint-globex'))
        .toBeInTheDocument());
    expect(mockApi.entityMentionsPreview).toHaveBeenLastCalledWith(
      expect.objectContaining({ type: 'organization', offset: 2, limit: 100 }),
    );
    // Organizations grew to 3; the date section did not gain a group.
    expect(within(screen.getByTestId('mentions-groups-organization')).getAllByTestId('mentions-group-row'))
      .toHaveLength(3);
    expect(within(screen.getByTestId('mentions-groups-date')).getAllByTestId('mentions-group-row'))
      .toHaveLength(1);
    // The heading still states the category total, refreshed from the response.
    expect(screen.getByTestId('mentions-type-toggle-organization')).toHaveTextContent('42');
  });

  it('ignores a Show more page that arrives after the search moved on', async () => {
    const SEARCHED = preview({
      search: 'zzz',
      items: [ZENITH],
      totalGroups: 1,
      typeTotals: [{ type: 'organization', totalGroups: 1 }],
    });
    let resolveStale!: (value: EntityMentionsPreview) => void;
    mockApi.entityMentionsPreview.mockImplementation(
      (input: { type?: string; search?: string }) => {
        if (input.type) {
          return new Promise<EntityMentionsPreview>((resolve) => { resolveStale = resolve; });
        }
        if (input.search) return Promise.resolve(SEARCHED);
        return Promise.resolve(MANY_ORGS);
      },
    );
    renderPanel();

    // Base page, then a Show more that will not resolve until after the search.
    fireEvent.click(await screen.findByTestId('mentions-show-more-organization'));
    fireEvent.change(screen.getByTestId('mentions-search'), { target: { value: 'zzz' } });
    await waitFor(() =>
      expect(mockApi.entityMentionsPreview).toHaveBeenCalledWith(
        expect.objectContaining({ search: 'zzz' }),
      ));
    await waitFor(() =>
      expect(screen.getByTestId('mentions-group-organization-fingerprint-zenith'))
        .toBeInTheDocument());

    // The stale organization page lands now, tagged with the pre-search query —
    // it must NOT append into the search results.
    await act(async () => { resolveStale(ORG_PAGE_TWO); });
    expect(screen.queryByTestId('mentions-group-organization-fingerprint-globex'))
      .not.toBeInTheDocument();
  });

  it('counts failures OUT of the extracted total and names them separately', async () => {
    // completed_rows (997) INCLUDES the 3 failures — 994 rows produced
    // mentions, and printing 997 would count the failures as successes.
    mockApi.entityMentionsPreview.mockResolvedValue(preview());
    renderPanel();

    await waitFor(() =>
      expect(screen.getByTestId('mentions-coverage')).toHaveTextContent(
        '994 of 1,000 rows extracted · 3 failed',
      ));
  });

  it('shows no coverage line at all when the column has never been run', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(
      preview({ coverage: { targetRows: null, completedRows: null, failedRows: null, sheetRows: 1000 } }),
    );
    renderPanel();

    await screen.findByTestId('mentions-type-organization');
    // "0 of 0 rows extracted" would be a claim about an extraction that never
    // happened.
    expect(screen.queryByTestId('mentions-coverage')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Staying current. `map.ner` creates its output column when the run STARTS, so
// an open panel's first preview is the empty one taken at that instant — the
// live bug was that it kept printing "0 of 4 rows extracted · No mentions
// found in this extraction" for as long as the panel stayed open after a
// finished 4/4 extraction. A specific wrong number is the failure mode this
// panel exists to avoid, so BOTH ways back to the truth are pinned here.
// ---------------------------------------------------------------------------

describe('MentionsPanel — staying current after a run', () => {
  const EMPTY = preview({
    coverage: { targetRows: 4, completedRows: 0, failedRows: 0, sheetRows: 4 },
    totalGroups: 0,
    typeTotals: [],
    items: [],
  });
  const FILLED = preview({
    coverage: { targetRows: 4, completedRows: 4, failedRows: 0, sheetRows: 4 },
    totalGroups: 1,
    typeTotals: [{ type: 'organization', totalGroups: 1 }],
    items: [ACME],
  });

  it('re-reads when the host reports the sheet data changed', async () => {
    mockApi.entityMentionsPreview
      .mockResolvedValueOnce(EMPTY)
      .mockResolvedValueOnce(FILLED);
    const { rerender } = render(
      <MentionsPanel
        sheets={[MARKED_SHEET]}
        activeSheetId="7"
        dataVersion={3}
      />,
    );

    await waitFor(() =>
      expect(screen.getByTestId('mentions-coverage')).toHaveTextContent('0 of 4 rows extracted'));
    expect(screen.getByTestId('mentions-summary')).toHaveTextContent(
      'No mentions found in this extraction',
    );

    // The run finished: the host bumps its data version.
    rerender(
      <MentionsPanel
        sheets={[MARKED_SHEET]}
        activeSheetId="7"
        dataVersion={4}
      />,
    );

    await waitFor(() =>
      expect(screen.getByTestId('mentions-coverage')).toHaveTextContent('4 of 4 rows extracted'));
    // The extraction now speaks through its section; the zero-state line is gone.
    expect(screen.getByTestId('mentions-type-organization')).toBeInTheDocument();
    expect(screen.queryByTestId('mentions-summary')).not.toBeInTheDocument();
    expect(mockApi.entityMentionsPreview).toHaveBeenCalledTimes(2);
  });

  it('does not re-read on a re-render that carries the same data version', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(FILLED);
    const { rerender } = render(
      <MentionsPanel sheets={[MARKED_SHEET]} activeSheetId="7" dataVersion={3}
        />,
    );
    await screen.findByTestId('mentions-type-organization');
    rerender(
      <MentionsPanel sheets={[MARKED_SHEET]} activeSheetId="7" dataVersion={3}
        />,
    );
    await waitFor(() => expect(mockApi.entityMentionsPreview).toHaveBeenCalledTimes(1));
  });

  it('offers an explicit refresh, so the user is never stuck with a stale number', async () => {
    mockApi.entityMentionsPreview
      .mockResolvedValueOnce(EMPTY)
      .mockResolvedValueOnce(FILLED);
    renderPanel();

    await waitFor(() =>
      expect(screen.getByTestId('mentions-coverage')).toHaveTextContent('0 of 4 rows extracted'));
    fireEvent.click(screen.getByTestId('mentions-refresh'));

    await waitFor(() =>
      expect(screen.getByTestId('mentions-coverage')).toHaveTextContent('4 of 4 rows extracted'));
  });

  it('re-reads the base page on refresh, dropping any appended category page', async () => {
    const MANY_ORGS = preview({
      items: [ACME, ZENITH, MARCH],
      totalGroups: 43,
      typeTotals: [
        { type: 'organization', totalGroups: 42 },
        { type: 'date', totalGroups: 1 },
      ],
    });
    const ORG_PAGE_TWO = preview({
      type: 'organization',
      items: [GLOBEX],
      totalGroups: 42,
      typeTotals: [{ type: 'organization', totalGroups: 42 }],
      offset: 2,
      limit: 100,
    });
    mockApi.entityMentionsPreview
      .mockResolvedValueOnce(MANY_ORGS)
      .mockResolvedValueOnce(ORG_PAGE_TWO)
      .mockResolvedValueOnce(MANY_ORGS);
    renderPanel();

    fireEvent.click(await screen.findByTestId('mentions-show-more-organization'));
    await waitFor(() =>
      expect(screen.getByTestId('mentions-group-organization-fingerprint-globex'))
        .toBeInTheDocument());

    // Refresh re-reads the UNTYPED base page from offset 0: the appended
    // organization page is dropped rather than counted against changed data.
    fireEvent.click(screen.getByTestId('mentions-refresh'));
    await waitFor(() =>
      expect(screen.queryByTestId('mentions-group-organization-fingerprint-globex'))
        .not.toBeInTheDocument());
    expect(mockApi.entityMentionsPreview).toHaveBeenLastCalledWith(
      expect.objectContaining({ limit: 25, offset: 0 }),
    );
  });
});

// ---------------------------------------------------------------------------
// A failed category page is that CATEGORY's failure, not the panel's. The base
// load owns the whole-panel error treatment because a base load that failed has
// nothing honest to show; a "Show more" that failed still has every loaded
// group behind it, and blanking them would read as data loss.
// ---------------------------------------------------------------------------

describe('MentionsPanel — a failed category page stays inside its section', () => {
  const MANY_ORGS = preview({
    items: [ACME, ZENITH, MARCH],
    totalGroups: 43,
    typeTotals: [
      { type: 'organization', totalGroups: 42 },
      { type: 'date', totalGroups: 1 },
    ],
  });
  const ORG_PAGE_TWO = preview({
    type: 'organization',
    items: [GLOBEX],
    totalGroups: 42,
    typeTotals: [{ type: 'organization', totalGroups: 42 }],
    offset: 2,
    limit: 100,
  });

  it('keeps the loaded groups and scopes the message to the asking section', async () => {
    mockApi.entityMentionsPreview
      .mockResolvedValueOnce(MANY_ORGS)
      .mockRejectedValueOnce(new ApiError(503, 'preview timed out', 'unavailable'));
    renderPanel();

    fireEvent.click(await screen.findByTestId('mentions-show-more-organization'));

    const error = await screen.findByTestId('mentions-show-more-error-organization');
    expect(error).toHaveTextContent('preview timed out');
    // The panel did NOT become an error page…
    expect(screen.queryByTestId('mentions-error')).not.toBeInTheDocument();
    // …the groups already in hand are still on screen…
    expect(screen.getByTestId('mentions-group-organization-fingerprint-acme-corp'))
      .toBeInTheDocument();
    expect(screen.getByTestId('mentions-group-organization-fingerprint-zenith'))
      .toBeInTheDocument();
    // …the untouched section is untouched, message and all…
    expect(screen.getByTestId('mentions-type-date')).toBeInTheDocument();
    expect(screen.queryByTestId('mentions-show-more-error-date')).not.toBeInTheDocument();
    // …and the button that failed is the way out, no longer spinning.
    const retry = screen.getByTestId('mentions-show-more-organization');
    expect(retry).toHaveTextContent('Try again');
    expect(retry).toBeEnabled();
  });

  it('clears the section error when the retry succeeds', async () => {
    mockApi.entityMentionsPreview
      .mockResolvedValueOnce(MANY_ORGS)
      .mockRejectedValueOnce(new ApiError(503, 'preview timed out', 'unavailable'))
      .mockResolvedValueOnce(ORG_PAGE_TWO);
    renderPanel();

    fireEvent.click(await screen.findByTestId('mentions-show-more-organization'));
    await screen.findByTestId('mentions-show-more-error-organization');

    fireEvent.click(screen.getByTestId('mentions-show-more-organization'));
    await waitFor(() =>
      expect(screen.getByTestId('mentions-group-organization-fingerprint-globex'))
        .toBeInTheDocument());
    expect(screen.queryByTestId('mentions-show-more-error-organization')).not.toBeInTheDocument();
  });

  it('drops a section error the panel has already left behind', async () => {
    mockApi.entityMentionsPreview
      .mockResolvedValueOnce(MANY_ORGS)
      .mockRejectedValueOnce(new ApiError(503, 'preview timed out', 'unavailable'))
      .mockResolvedValueOnce(MANY_ORGS);
    renderPanel();

    fireEvent.click(await screen.findByTestId('mentions-show-more-organization'));
    await screen.findByTestId('mentions-show-more-error-organization');

    // A refresh is a new query universe; the old page's failure is not its error.
    fireEvent.click(screen.getByTestId('mentions-refresh'));
    await waitFor(() =>
      expect(screen.queryByTestId('mentions-show-more-error-organization'))
        .not.toBeInTheDocument());
  });
});

// ---------------------------------------------------------------------------
// …and NOT re-reading when nothing about the data changed. The panel's own
// primary action used to trigger the re-read above: applying a mention filter
// bumped the host's dataVersion (useWorkspaceModel.tsx), which the panel reads
// as "this extraction changed". Mention counts are whole-sheet aggregates that
// a grid filter cannot move, so the filter no longer bumps it — the grid's row
// cache re-keys on the filter itself (grid/rowCacheStore.ts computeRowCacheKey).
// ---------------------------------------------------------------------------

describe('MentionsPanel — a grid filter is not a data change', () => {
  it('keeps every loaded category page when the applied mention filter changes', async () => {
    const MANY_ORGS = preview({
      items: [ACME, ZENITH, MARCH],
      totalGroups: 43,
      typeTotals: [
        { type: 'organization', totalGroups: 42 },
        { type: 'date', totalGroups: 1 },
      ],
    });
    const ORG_PAGE_TWO = preview({
      type: 'organization',
      items: [GLOBEX],
      totalGroups: 42,
      typeTotals: [{ type: 'organization', totalGroups: 42 }],
      offset: 2,
      limit: 100,
    });
    mockApi.entityMentionsPreview
      .mockResolvedValueOnce(MANY_ORGS)
      .mockResolvedValueOnce(ORG_PAGE_TWO);
    const onFilter = vi.fn();
    const { rerender } = render(
      <MentionsPanel
        sheets={[MARKED_SHEET]}
        activeSheetId="7"
        gridFilter={null}
        dataVersion={2}
        onFilterEntity={onFilter}
      />,
    );

    fireEvent.click(await screen.findByTestId('mentions-show-more-organization'));
    const globex = await screen.findByTestId('mentions-group-organization-fingerprint-globex');

    // Clicking the appended group: the host applies the filter and leaves the
    // data version alone, because the rows behind the sheet did not change.
    fireEvent.click(globex);
    expect(onFilter).toHaveBeenCalledWith(
      '12',
      { type: 'organization', fingerprint: 'globex' },
      'Globex',
    );
    rerender(
      <MentionsPanel
        sheets={[MARKED_SHEET]}
        activeSheetId="7"
        gridFilter={{ entities: { entity_eq: { type: 'organization', fingerprint: 'globex' } } }}
        dataVersion={2}
        onFilterEntity={onFilter}
      />,
    );

    // The appended group the user just clicked is still on screen, still active.
    expect(screen.getByTestId('mentions-group-organization-fingerprint-globex'))
      .toHaveAttribute('aria-pressed', 'true');
    expect(within(screen.getByTestId('mentions-groups-organization')).getAllByTestId('mentions-group-row'))
      .toHaveLength(3);
    expect(mockApi.entityMentionsPreview).toHaveBeenCalledTimes(2);
  });
});

// ---------------------------------------------------------------------------
// Type headings read as the words the NER form offered.
// ---------------------------------------------------------------------------

describe('MentionsPanel — readable type names', () => {
  const FAC: EntityMentionGroup = {
    type: 'fac',
    selector: { kind: 'text', text: 'Penn Station' },
    label: 'Penn Station',
    rowCount: 2,
    mentionCount: 2,
    surfaceCount: 1,
    surfaces: [{ text: 'Penn Station', rowCount: 2, mentionCount: 2 }],
  };
  const CUSTOM: EntityMentionGroup = {
    ...FAC,
    type: 'medical_condition',
    selector: { kind: 'text', text: 'sarcoidosis' },
    label: 'sarcoidosis',
    surfaces: [{ text: 'sarcoidosis', rowCount: 2, mentionCount: 2 }],
  };

  it('names a canonical type the way the NER form did, not as its tag', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(
      preview({ items: [FAC], totalGroups: 1 }),
    );
    renderPanel({ onFilterEntity: vi.fn() });

    // The section is still IDENTIFIED by the raw canonical type…
    const section = await screen.findByTestId('mentions-type-fac');
    // …and PRESENTED with the words the user checked in the form.
    expect(section).toHaveTextContent('Facilities');
    expect(section).not.toHaveTextContent('FAC');
    expect(screen.getByTestId('mentions-type-filter-fac')).toHaveAccessibleName(
      'Filter the grid to rows with any Facilities mention',
    );
  });

  it('humanizes an arbitrary GLiNER/LLM type rather than dropping it', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(
      preview({ items: [CUSTOM], totalGroups: 1 }),
    );
    renderPanel({ onFilterEntity: vi.fn() });

    const section = await screen.findByTestId('mentions-type-medical_condition');
    expect(section).toHaveTextContent('Medical condition');
  });
});

// ---------------------------------------------------------------------------
// Rows added after the extraction. `map.ner` refuses run.backfill, so the gap
// is permanent until a whole-sheet re-run and the panel has to say so.
// ---------------------------------------------------------------------------

describe('MentionsPanel — an extraction that no longer covers the sheet', () => {
  it('says how many rows are uncovered and what to do about it', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(
      preview({ coverage: { targetRows: 2, completedRows: 2, failedRows: 0, sheetRows: 4 } }),
    );
    renderPanel();

    const line = await screen.findByTestId('mentions-coverage');
    expect(line).toHaveTextContent('2 of 2 rows extracted');
    expect(line).toHaveTextContent(
      '2 rows on this sheet not extracted — re-run extraction to cover the whole sheet',
    );
    expect(line).toHaveAttribute('data-uncovered', 'true');
  });

  it('adds no noise when the extraction still covers the sheet', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(
      preview({ coverage: { targetRows: 4, completedRows: 4, failedRows: 0, sheetRows: 4 } }),
    );
    renderPanel();

    const line = await screen.findByTestId('mentions-coverage');
    expect(line).toHaveTextContent('4 of 4 rows extracted');
    expect(line).not.toHaveTextContent('re-run');
    expect(line).not.toHaveAttribute('data-uncovered');
  });

  it('still shows no coverage line at all for a column that was never run', async () => {
    mockApi.entityMentionsPreview.mockResolvedValue(
      preview({
        coverage: { targetRows: null, completedRows: null, failedRows: null, sheetRows: 4 },
      }),
    );
    renderPanel();

    await screen.findByTestId('mentions-type-organization');
    expect(screen.queryByTestId('mentions-coverage')).not.toBeInTheDocument();
  });
});
