// @vitest-environment jsdom
//
// The mention-detail panel shows which documents contain one normalized
// mention.
//
// Pins the things that make it a HONEST drill-down rather than a second
// opinion: the headline is the clicked spelling (never the fingerprint, which
// nobody wrote), the counts are mentions-then-documents in the Mentions panel's
// own order, the two identity modes travel end to end, and opening a result
// does not close the panel.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      entityMentionDocuments: vi.fn(),
      entityMentionOccurrences: vi.fn(),
    },
  };
});

import { MentionDetailPanel } from '../../src/workbench/MentionDetailPanel';
import type { MentionDetailTarget } from '../../src/workbench/mentionDetailModel';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('test-project');

import type {
  EntityMentionDocumentsPage,
  EntityMentionOccurrencesPage,
} from '../../src/api/open';

const entityMentionDocuments = vi.spyOn(api, 'entityMentionDocuments');
const entityMentionOccurrences = vi.spyOn(api, 'entityMentionOccurrences');
const mockApi = { entityMentionDocuments, entityMentionOccurrences };
const { render } = createWorkspaceTestHarness({
  projectId: 'test-project', api: { projectApi: api },
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const FINGERPRINTED: MentionDetailTarget = {
  sheetId: '7',
  columnId: '12',
  type: 'person',
  fingerprint: 'ada lovelace',
  label: 'A. Lovelace',
};

const UNFINGERPRINTED: MentionDetailTarget = {
  sheetId: '7',
  columnId: '12',
  type: 'money',
  fingerprint: null,
  label: '$1,250,000',
};

function page(overrides: Partial<EntityMentionDocumentsPage> = {}): EntityMentionDocumentsPage {
  return {
    sheetId: '7',
    columnId: '12',
    type: 'person',
    selector: { kind: 'fingerprint', fingerprint: 'ada lovelace' },
    totals: { mentions: 14, documents: 5 },
    documents: [
      { rowId: '3', title: 'Board minutes, March', occurrenceCount: 6 },
      { rowId: '1', title: 'Interview — Session 3', occurrenceCount: 4 },
      { rowId: '9', title: null, occurrenceCount: 1 },
    ],
    nextOffset: null,
    ...overrides,
  };
}

function renderPanel(
  target = FINGERPRINTED,
  overrides: Partial<Parameters<typeof MentionDetailPanel>[0]> = {},
) {
  return render(
    <MentionDetailPanel
      target={target}
      activeRowId="1"
      onClose={() => {}}
      onOpenDocument={() => {}}
      {...overrides}
    />,
  );
}

describe('MentionDetailPanel', () => {
  it('headlines the clicked SPELLING and its type, never the fingerprint', async () => {
    mockApi.entityMentionDocuments.mockResolvedValue(page());
    renderPanel();

    const title = await screen.findByTestId('mention-detail-title');
    // "A. Lovelace" is what was clicked; "ada lovelace" is an internal
    // comparison token and must never reach the screen.
    expect(title).toHaveTextContent('“A. Lovelace”');
    // The SINGULAR display name: the panel describes ONE mention, so the NER
    // form's plural ("Ada Lovelace · People") would read wrong.
    expect(title).toHaveTextContent('Person');
    expect(title.textContent).not.toContain('ada lovelace');
  });

  it('states mentions across documents, in the Mentions panel’s own order', async () => {
    mockApi.entityMentionDocuments.mockResolvedValue(page());
    renderPanel();
    // mention_count (occurrences) LEFT, row_count (documents) RIGHT — the same
    // convention the Mentions panel uses, so the two surfaces read alike.
    expect(await screen.findByTestId('mention-detail-counts')).toHaveTextContent(
      '14 mentions across 5 documents',
    );
  });

  it('sends a FINGERPRINT identity for a fingerprinted type', async () => {
    mockApi.entityMentionDocuments.mockResolvedValue(page());
    renderPanel();
    await screen.findByTestId('mention-detail-counts');
    expect(mockApi.entityMentionDocuments).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'person', fingerprint: 'ada lovelace' }),
    );
    expect(mockApi.entityMentionDocuments.mock.calls[0][0]).not.toHaveProperty('text');
    expect(screen.getByTestId('mention-detail-basis')).toHaveTextContent(
      'Every spelling that normalizes to this mention.',
    );
  });

  it('sends a TEXT identity for an unfingerprinted type, and says spellings are not merged', async () => {
    // "$1,250,000" and "$1.25M" are different mentions here, and the panel must
    // never imply otherwise (R-unfingerprinted).
    mockApi.entityMentionDocuments.mockResolvedValue(
      page({ type: 'money', selector: { kind: 'text', text: '$1,250,000' } }),
    );
    renderPanel(UNFINGERPRINTED);
    await screen.findByTestId('mention-detail-counts');

    expect(mockApi.entityMentionDocuments).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'money', text: '$1,250,000' }),
    );
    expect(mockApi.entityMentionDocuments.mock.calls[0][0]).not.toHaveProperty('fingerprint');
    expect(screen.getByTestId('mention-detail-basis')).toHaveTextContent('never merged');
  });

  it('opens a document WITHOUT closing itself', async () => {
    const opened: string[] = [];
    const closed = vi.fn();
    mockApi.entityMentionDocuments.mockResolvedValue(page());
    renderPanel(FINGERPRINTED, {
      onOpenDocument: (rowId) => opened.push(rowId),
      onClose: closed,
    });

    const docs = await screen.findAllByTestId('mention-detail-doc');
    fireEvent.click(docs[0]!);
    expect(opened).toEqual(['3']);
    // Docked (O3): a drill-down that closes on every click is a navigation.
    expect(closed).not.toHaveBeenCalled();
    expect(screen.getByTestId('mention-detail-panel')).toBeInTheDocument();
  });

  it('names a title-less row by its id rather than dropping it', async () => {
    mockApi.entityMentionDocuments.mockResolvedValue(page());
    renderPanel();
    const docs = await screen.findAllByTestId('mention-detail-doc');
    expect(docs).toHaveLength(3);
    expect(docs[2]).toHaveTextContent('Row 9');
  });

  it('pages Level 1 without recounting the totals', async () => {
    mockApi.entityMentionDocuments
      .mockResolvedValueOnce(
        page({ documents: [{ rowId: '3', title: 'First', occurrenceCount: 6 }], nextOffset: 1 }),
      )
      .mockResolvedValueOnce(
        page({ documents: [{ rowId: '1', title: 'Second', occurrenceCount: 4 }], nextOffset: null }),
      );
    renderPanel();

    fireEvent.click(await screen.findByTestId('mention-detail-more'));
    await waitFor(() => expect(screen.getAllByTestId('mention-detail-doc')).toHaveLength(2));
    expect(mockApi.entityMentionDocuments).toHaveBeenLastCalledWith(
      expect.objectContaining({ offset: 1 }),
    );
    // Totals are full-group facts; only the list grew.
    expect(screen.getByTestId('mention-detail-counts')).toHaveTextContent(
      '14 mentions across 5 documents',
    );
  });

  it('surfaces a failed lookup instead of an empty list', async () => {
    mockApi.entityMentionDocuments.mockRejectedValue(new Error('column_not_entity_mentions'));
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId('mention-detail-error')).toHaveTextContent(
        'column_not_entity_mentions',
      ),
    );
  });
});

// ---------------------------------------------------------------------------
// Occurrences within one document, fetched only on expand.
// ---------------------------------------------------------------------------

function occurrences(
  overrides: Partial<EntityMentionOccurrencesPage> = {},
): EntityMentionOccurrencesPage {
  return {
    sheetId: '7',
    rowId: '3',
    columnId: '12',
    type: 'person',
    selector: { kind: 'fingerprint', fingerprint: 'ada lovelace' },
    textColumn: { id: '11', name: 'body' },
    totals: { occurrences: 6 },
    occurrences: [
      {
        occurrenceId: '41:9',
        start: 26,
        end: 38,
        quote: 'Ada Lovelace',
        snippet: {
          text: 'convened by Ada Lovelace to review',
          markStart: 12,
          markEnd: 24,
          truncatedStart: true,
          truncatedEnd: true,
        },
      },
      {
        occurrenceId: '42:9',
        start: 90,
        end: 101,
        quote: 'A. Lovelace',
        snippet: {
          text: 'motion from A. Lovelace was carried',
          markStart: 12,
          markEnd: 23,
          truncatedStart: true,
          truncatedEnd: false,
        },
      },
    ],
    nextOffset: null,
    unpositioned: null,
    ...overrides,
  };
}

describe('MentionDetailPanel — Level 2', () => {
  it('fetches nothing until a document is expanded, then only for that document', async () => {
    mockApi.entityMentionDocuments.mockResolvedValue(page());
    mockApi.entityMentionOccurrences.mockResolvedValue(occurrences());
    renderPanel();

    const expanders = await screen.findAllByTestId('mention-detail-expand');
    // Three documents listed, zero cell fetches: the whole reason Level 2 is
    // lazy (R-snippet-cost).
    expect(mockApi.entityMentionOccurrences).not.toHaveBeenCalled();

    fireEvent.click(expanders[0]!);
    await screen.findByTestId('mention-occurrence-list');
    expect(mockApi.entityMentionOccurrences).toHaveBeenCalledTimes(1);
    expect(mockApi.entityMentionOccurrences).toHaveBeenCalledWith(
      expect.objectContaining({ rowId: '3', columnId: '12', fingerprint: 'ada lovelace' }),
    );
  });

  it('draws the snippet mark at the server’s offsets, not by searching for the quote', async () => {
    mockApi.entityMentionDocuments.mockResolvedValue(page());
    mockApi.entityMentionOccurrences.mockResolvedValue(occurrences());
    renderPanel();
    fireEvent.click((await screen.findAllByTestId('mention-detail-expand'))[0]!);

    const rows = await screen.findAllByTestId('mention-occurrence');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('convened by Ada Lovelace to review');
    // Both spellings of the fingerprint group are occurrences here; the mark
    // is whatever the offsets cover, which is why the second one is the OTHER
    // spelling and still correct.
    expect(rows[0]!.querySelector('mark')?.textContent).toBe('Ada Lovelace');
    expect(rows[1]!.querySelector('mark')?.textContent).toBe('A. Lovelace');
  });

  it('collapsing hides the occurrences and issues no fetch', async () => {
    mockApi.entityMentionDocuments.mockResolvedValue(page());
    mockApi.entityMentionOccurrences.mockResolvedValue(occurrences());
    renderPanel();
    const expander = (await screen.findAllByTestId('mention-detail-expand'))[0]!;

    fireEvent.click(expander);
    await screen.findByTestId('mention-occurrence-list');
    expect(expander).toHaveAttribute('aria-expanded', 'true');

    fireEvent.click(expander);
    expect(screen.queryByTestId('mention-occurrence-list')).not.toBeInTheDocument();
    expect(expander).toHaveAttribute('aria-expanded', 'false');
    expect(mockApi.entityMentionOccurrences).toHaveBeenCalledTimes(1);
  });

  it('opens the document AT the clicked occurrence', async () => {
    const opened: Array<[string, string | undefined]> = [];
    mockApi.entityMentionDocuments.mockResolvedValue(page());
    mockApi.entityMentionOccurrences.mockResolvedValue(occurrences());
    renderPanel(FINGERPRINTED, {
      onOpenDocument: (rowId, occurrenceId) => opened.push([rowId, occurrenceId]),
    });
    fireEvent.click((await screen.findAllByTestId('mention-detail-expand'))[0]!);

    fireEvent.click((await screen.findAllByTestId('mention-occurrence'))[1]!);
    expect(opened).toEqual([['3', '42:9']]);
    // Clicking the TITLE is the other question, and carries no occurrence.
    fireEvent.click(screen.getAllByTestId('mention-detail-doc')[0]!);
    expect(opened[1]).toEqual(['3', undefined]);
  });

  it('pages occurrences within one document', async () => {
    mockApi.entityMentionDocuments.mockResolvedValue(page());
    mockApi.entityMentionOccurrences
      .mockResolvedValueOnce(occurrences({ nextOffset: 2 }))
      .mockResolvedValueOnce(
        occurrences({
          nextOffset: null,
          occurrences: [
            {
              occurrenceId: '43:9',
              start: 200,
              end: 212,
              quote: 'Ada Lovelace',
              snippet: {
                text: 'Ada Lovelace abstained',
                markStart: 0,
                markEnd: 12,
                truncatedStart: false,
                truncatedEnd: true,
              },
            },
          ],
        }),
      );
    renderPanel();
    fireEvent.click((await screen.findAllByTestId('mention-detail-expand'))[0]!);

    const more = await screen.findByTestId('mention-occurrence-more');
    // 6 in the document, 2 shown.
    expect(more).toHaveTextContent('See 4 more in this document');
    fireEvent.click(more);
    await waitFor(() =>
      expect(screen.getAllByTestId('mention-occurrence')).toHaveLength(3),
    );
    expect(mockApi.entityMentionOccurrences).toHaveBeenLastCalledWith(
      expect.objectContaining({ offset: 2 }),
    );
  });

  it('says the text moved rather than saying the mention is absent', async () => {
    mockApi.entityMentionDocuments.mockResolvedValue(page());
    mockApi.entityMentionOccurrences.mockResolvedValue(
      occurrences({
        occurrences: [],
        totals: { occurrences: 0 },
        unpositioned: { reason: 'content_hash_mismatch', total: 6 },
      }),
    );
    renderPanel();
    fireEvent.click((await screen.findAllByTestId('mention-detail-expand'))[0]!);

    const note = await screen.findByTestId('mention-occurrence-unpositioned');
    expect(note).toHaveTextContent('The source text changed');
    // No snippets drawn at coordinates that stopped applying.
    expect(screen.queryByTestId('mention-occurrence')).not.toBeInTheDocument();
  });

  it('a failed expand reports itself and leaves the rest of the panel usable', async () => {
    mockApi.entityMentionDocuments.mockResolvedValue(page());
    mockApi.entityMentionOccurrences.mockRejectedValue(new Error('sheet_not_visible'));
    renderPanel();
    fireEvent.click((await screen.findAllByTestId('mention-detail-expand'))[0]!);

    expect(await screen.findByTestId('mention-occurrence-error')).toHaveTextContent(
      'sheet_not_visible',
    );
    expect(screen.getAllByTestId('mention-detail-doc')).toHaveLength(3);
  });
});
