// @vitest-environment jsdom
//
// The annotated-text reader displays a long text cell with the
// pipeline's entities marked in place.
//
// Pins the four things that are easy to get silently wrong:
//   * marks are drawn at the SERVER's offsets over the SERVER's string — never
//     re-located by searching for the quote, which is what EvidenceViewer does
//     and which puts the mark on the wrong occurrence of a repeated name;
//   * a nested pair stays two marks with the inner one owning the click;
//   * an UNPOSITIONED layer keeps its count and draws NOTHING — a stale layer
//     rendering its stored offsets is the confident-wrong highlight the whole
//     coordinate substrate exists to prevent;
//   * the toggle is the persisted DISABLED set, so a layer that appears later
//     defaults to visible.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    api: { ...actual.api, getTextAnnotations: vi.fn() },
  };
});

import { AnnotatedTextReader } from '../../src/workbench/AnnotatedTextReader';

import type { TextAnnotationLayer, TextAnnotations } from '../../src/api/open';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('test-project');
const getTextAnnotations = vi.spyOn(api, 'getTextAnnotations');
const mockApi = { getTextAnnotations };
const { render } = createWorkspaceTestHarness({
  projectId: 'test-project', api: { projectApi: api },
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// "Ada" appears TWICE. A renderer that located quotes by search would mark the
// first one for both spans; the offsets say otherwise.
const TEXT = 'Ada Lovelace wrote to Ada about Boeing Company Ltd.';

const ENTITIES_LAYER: TextAnnotationLayer = {
  toggleKey: '7:12:entities',
  layerFamily: 'entities',
  producerKind: 'map.ner',
  producerEngine: 'gliner',
  outputColumn: { id: '12', name: 'entities' },
  positioned: true,
  spans: [
    { occurrenceId: '1:1', start: 0, end: 12, quote: 'Ada Lovelace', entityType: 'person' },
    { occurrenceId: '2:1', start: 22, end: 25, quote: 'Ada', entityType: 'person' },
    {
      occurrenceId: '3:1',
      start: 32,
      end: 50,
      quote: 'Boeing Company Ltd',
      entityType: 'organization',
    },
    // Nested, and in the MIDDLE of its container — so the outer mark is drawn
    // as two fragments with the inner one between them.
    { occurrenceId: '4:1', start: 39, end: 46, quote: 'Company', entityType: 'organization' },
  ],
  counts: { shown: 4, total: 4, invalid: 0 },
};

const DIRTY_LAYER: TextAnnotationLayer = {
  toggleKey: '7:12:entities',
  layerFamily: 'entities',
  producerKind: 'map.ner',
  producerEngine: 'spacy',
  outputColumn: { id: '12', name: 'entities' },
  positioned: false,
  unpositioned: { reason: 'content_hash_mismatch', total: 4 },
};

function payload(layers: TextAnnotationLayer[], text: string | null = TEXT): TextAnnotations {
  return {
    sheetId: '7',
    rowId: '3',
    columnId: '11',
    text,
    contentHash: text === null ? null : 'sha256:abc',
    layers,
  };
}

function renderReader(overrides: Partial<Parameters<typeof AnnotatedTextReader>[0]> = {}) {
  return render(
    <AnnotatedTextReader
      rowId="3"
      columnId="11"
      title="Interview — Session 3"
      disabledToggleKeys={[]}
      onSetDisabledToggleKeys={() => {}}
      onOpenDetail={() => {}}
      canOpenDetail
      optionsOpen={false}
      onToggleOptions={() => {}}
      optionsPopover={null}
      selectionCount={0}
      {...overrides}
    />,
  );
}

describe('AnnotatedTextReader', () => {
  it('renders the exact text and marks it at the SERVER offsets', async () => {
    mockApi.getTextAnnotations.mockResolvedValue(payload([ENTITIES_LAYER]));
    renderReader();

    const body = await screen.findByTestId('annotated-text-content');
    // The string is reproduced character-for-character: the marks partition it,
    // they do not rewrite it.
    expect(body.textContent).toBe(TEXT);

    // The SECOND "Ada" is marked as its own occurrence — the give-away that
    // offsets, not a quote search, placed it.
    const marks = screen.getAllByTestId('annotation-mark');
    const secondAda = marks.find((m) => m.dataset.occurrenceId === '2:1');
    expect(secondAda).toBeDefined();
    expect(secondAda).toHaveTextContent('Ada');
    expect(body.textContent!.indexOf('Ada', 1)).toBe(22);
  });

  it('fetches only the ACTIVE cell', async () => {
    mockApi.getTextAnnotations.mockResolvedValue(payload([ENTITIES_LAYER]));
    renderReader();
    await screen.findByTestId('annotated-text-content');
    expect(mockApi.getTextAnnotations).toHaveBeenCalledTimes(1);
    expect(mockApi.getTextAnnotations).toHaveBeenCalledWith('3', '11');
  });

  it('keeps a NESTED pair as two marks, with the inner one owning the click', async () => {
    const opened: string[] = [];
    mockApi.getTextAnnotations.mockResolvedValue(payload([ENTITIES_LAYER]));
    renderReader({ onOpenMention: (mark) => opened.push(mark.occurrenceId) });

    await screen.findByTestId('annotated-text-content');
    const marks = screen.getAllByTestId('annotation-mark');
    const inner = marks.find((m) => m.textContent === 'Company')!;
    // "Company" is INSIDE "Boeing Company Ltd": the inner span owns the click
    // there, and the outer one still owns the text on either side of it.
    expect(inner.dataset.occurrenceId).toBe('4:1');
    expect(inner.dataset.depth).toBe('2');
    expect(marks.find((m) => m.textContent === 'Boeing ')!.dataset.occurrenceId).toBe('3:1');
    expect(marks.find((m) => m.textContent === ' Ltd')!.dataset.occurrenceId).toBe('3:1');

    fireEvent.click(inner);
    expect(opened).toEqual(['4:1']);
  });

  it('draws NOTHING for an unpositioned layer but keeps its count and says why', async () => {
    mockApi.getTextAnnotations.mockResolvedValue(payload([DIRTY_LAYER]));
    renderReader();

    const body = await screen.findByTestId('annotated-text-content');
    expect(body.textContent).toBe(TEXT);
    expect(screen.queryAllByTestId('annotation-mark')).toHaveLength(0);
    // The last-known count survives — "here is what I knew, and why I can't
    // show it" rather than silence.
    expect(screen.getByTestId('annotation-toggle-7:12:entities')).toHaveTextContent('4');

    fireEvent.click(screen.getByTestId('annotation-status-7:12:entities'));
    expect(await screen.findByTestId('annotation-status-note')).toHaveTextContent(
      'The source text changed after this result was created',
    );
  });

  it('hides a layer whose toggle key is in the persisted DISABLED set', async () => {
    mockApi.getTextAnnotations.mockResolvedValue(payload([ENTITIES_LAYER]));
    renderReader({ disabledToggleKeys: ['7:12:entities'] });

    await screen.findByTestId('annotated-text-content');
    expect(screen.queryAllByTestId('annotation-mark')).toHaveLength(0);
    // Still listed, still counted — off, not gone.
    expect(screen.getByTestId('annotation-toggle-7:12:entities')).toBeInTheDocument();
  });

  it('writes the DISABLED set, so an unseen future layer defaults to visible', async () => {
    const writes: string[][] = [];
    mockApi.getTextAnnotations.mockResolvedValue(payload([ENTITIES_LAYER]));
    renderReader({ onSetDisabledToggleKeys: (next) => writes.push(next) });

    await screen.findByTestId('annotated-text-content');
    fireEvent.click(
      screen.getByRole('button', { name: /Entities/ }),
    );
    expect(writes).toEqual([['7:12:entities']]);
  });

  it('only the first fragment of a split occurrence is a tab stop', async () => {
    mockApi.getTextAnnotations.mockResolvedValue(payload([ENTITIES_LAYER]));
    renderReader({ onOpenMention: () => {} });

    await screen.findByTestId('annotated-text-content');
    const marks = screen.getAllByTestId('annotation-mark');
    const tabbable = marks.filter((m) => m.getAttribute('tabindex') === '0');
    // Four occurrences, four tab stops — even though "Boeing Company Ltd" is
    // drawn as two fragments around the nested "Company".
    expect(tabbable).toHaveLength(4);
    expect(marks).toHaveLength(5);
  });

  it('says so when the cell has no text at all', async () => {
    mockApi.getTextAnnotations.mockResolvedValue(payload([], null));
    renderReader();
    expect(await screen.findByTestId('annotated-text-empty')).toHaveTextContent(
      'This row has no text in the source column.',
    );
  });

  it('surfaces a failed load instead of an empty reader', async () => {
    mockApi.getTextAnnotations.mockRejectedValue(new Error('network is down'));
    renderReader();
    await waitFor(() =>
      expect(screen.getByTestId('annotated-text-error')).toHaveTextContent('network is down'),
    );
  });
});

// ---------------------------------------------------------------------------
// Accessibility contract for a fragmented, overlapping mark layer.
// ---------------------------------------------------------------------------

describe('AnnotatedTextReader — the [!] replay affordance', () => {
  it('offers the re-run and hands back the layer’s OUTPUT column', async () => {
    const replayed: string[] = [];
    mockApi.getTextAnnotations.mockResolvedValue(payload([DIRTY_LAYER]));
    renderReader({ onReplayLayer: (columnId) => replayed.push(columnId) });

    fireEvent.click(await screen.findByTestId('annotation-status-7:12:entities'));
    const note = await screen.findByTestId('annotation-status-note');
    expect(note).toHaveTextContent('The source text changed after this result was created');
    // Ruling 4 honesty: map.ner is a whole-column contract, so the affordance
    // must say the re-run rebuilds every row BEFORE the click — it reads as
    // repairing one stale document otherwise — AND that doing so is a fresh
    // purchase, since "re-run" otherwise reads like a resume that only pays
    // for what it repairs.
    expect(note).toHaveTextContent(
      'every row in the sheet runs again, not just this document',
    );
    expect(note).toHaveTextContent(
      'a new run, not a resume: the whole column is charged again',
    );

    fireEvent.click(screen.getByTestId('annotation-replay-7:12:entities'));
    // The output column, not the toggle key: that is where the stored action
    // is found; replay starts at output_column.id.
    expect(replayed).toEqual(['12']);
  });

  it('explains without a button when the host cannot launch runs', async () => {
    mockApi.getTextAnnotations.mockResolvedValue(payload([DIRTY_LAYER]));
    renderReader({ onReplayLayer: null });

    fireEvent.click(await screen.findByTestId('annotation-status-7:12:entities'));
    // Points at what to do rather than offering an affordance it cannot honor.
    expect(await screen.findByTestId('annotation-status-note')).toHaveTextContent(
      'Re-run the extraction for this column',
    );
    expect(screen.queryByTestId('annotation-replay-7:12:entities')).not.toBeInTheDocument();
  });

  it('offers nothing to re-run on a healthy layer', async () => {
    mockApi.getTextAnnotations.mockResolvedValue(payload([ENTITIES_LAYER]));
    renderReader({ onReplayLayer: () => {} });
    await screen.findByTestId('annotated-text-content');
    expect(screen.queryByTestId('annotation-status-7:12:entities')).not.toBeInTheDocument();
  });
});

describe('AnnotatedTextReader — accessibility (R18)', () => {
  it('a clickable mark is a real button, so Enter and Space are the element’s job', async () => {
    const opened: string[] = [];
    mockApi.getTextAnnotations.mockResolvedValue(payload([ENTITIES_LAYER]));
    renderReader({ onOpenMention: (mark) => opened.push(mark.occurrenceId) });

    await screen.findByTestId('annotated-text-content');
    const inner = screen
      .getAllByTestId('annotation-mark')
      .find((m) => m.textContent === 'Company')!;
    // Not a <mark role="button"> with a hand-rolled keydown: the element IS the
    // control, which is what gives it focus order and keyboard activation for
    // free — and what the click test above already exercises.
    expect(inner.tagName).toBe('BUTTON');
    expect(inner).toHaveAttribute('type', 'button');
    expect(
      screen.getByRole('button', {
        name: 'Company, organization, within Boeing Company Ltd, organization',
      }),
    ).toBe(inner);
    // A continuation fragment is still activatable (clicking the visible tail
    // of a mark must work) and still NAMED — its own text is its accessible
    // name, so it is never a nameless control.
    const tail = screen
      .getAllByTestId('annotation-mark')
      .find((m) => m.textContent === ' Ltd')!;
    expect(screen.getByRole('button', { name: 'Ltd' })).toBe(tail);
  });

  it('a read-only reader draws marks that are NOT controls', async () => {
    mockApi.getTextAnnotations.mockResolvedValue(payload([ENTITIES_LAYER]));
    // No onOpenMention: nothing to activate, so nothing claims to be
    // activatable and nothing enters tab order.
    renderReader({ onOpenMention: null });

    await screen.findByTestId('annotated-text-content');
    const marks = screen.getAllByTestId('annotation-mark');
    expect(marks.every((m) => m.tagName === 'MARK')).toBe(true);
    expect(marks.some((m) => m.getAttribute('tabindex') === '0')).toBe(false);
  });

  it('announces an overlap deterministically, innermost first', async () => {
    mockApi.getTextAnnotations.mockResolvedValue(payload([ENTITIES_LAYER]));
    renderReader({ onOpenMention: () => {} });

    await screen.findByTestId('annotated-text-content');
    const inner = screen
      .getAllByTestId('annotation-mark')
      .find((m) => m.textContent === 'Company')!;
    // The mark you are on, then what it sits inside — so a reader is told
    // "Company is inside Boeing Company Ltd" instead of being told twice about
    // an overlap it cannot see.
    expect(inner).toHaveAttribute(
      'aria-label',
      'Company, organization, within Boeing Company Ltd, organization',
    );
    // A continuation fragment is not separately named: it is not a tab stop.
    const tail = screen
      .getAllByTestId('annotation-mark')
      .find((m) => m.textContent === ' Ltd')!;
    expect(tail).not.toHaveAttribute('aria-label');
  });

  it('the toggle strip is a named group and the stale note is a live region', async () => {
    mockApi.getTextAnnotations.mockResolvedValue(payload([DIRTY_LAYER]));
    renderReader();

    // A <fieldset>/<legend>, not role="group" + aria-label.
    const group = await screen.findByTestId('annotation-toggles');
    expect(group.tagName).toBe('FIELDSET');
    expect(screen.getByRole('group', { name: 'Annotation layers' })).toBe(group);

    fireEvent.click(screen.getByTestId('annotation-status-7:12:entities'));
    const note = await screen.findByTestId('annotation-status-note');
    // <output> is role="status" in HTML: the live region comes from the tag.
    expect(note.tagName).toBe('OUTPUT');
    expect(screen.getByRole('status')).toBe(note);
  });
});
