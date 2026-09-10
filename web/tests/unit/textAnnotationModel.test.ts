// The annotated-text reader's pure model. What is pinned here is the D2
// partition, because it is the decision the whole renderer rests on and the one
// with a tempting wrong answer: merging overlapping marks into one highlight
// (what EvidenceViewer's `highlightQuotes` does) is cheaper, and it destroys
// exactly the case the reader exists for — "Boeing" nested inside "Boeing
// Company", where a merged highlight can answer neither which mention you
// clicked nor how many there are.

import { describe, expect, it } from 'vitest';

import {
  annotationToggles,
  collectMarks,
  layerFamilyLabel,
  partitionAnnotationFragments,
  type AnnotationMark,
} from '../../src/workbench/textAnnotationModel';
import type { TextAnnotationLayer } from '../../src/api/open';



function mark(
  occurrenceId: string,
  start: number,
  end: number,
  overrides: Partial<AnnotationMark> = {},
): AnnotationMark {
  return {
    occurrenceId,
    toggleKey: '7:12:entities',
    layerFamily: 'entities',
    entityType: 'organization',
    start,
    end,
    quote: '',
    ...overrides,
  };
}

function positionedLayer(
  spans: { occurrenceId: string; start: number; end: number; entityType?: string }[],
  overrides: Partial<Extract<TextAnnotationLayer, { positioned: true }>> = {},
): TextAnnotationLayer {
  return {
    toggleKey: '7:12:entities',
    layerFamily: 'entities',
    producerKind: 'map.ner',
    producerEngine: 'gliner',
    outputColumn: { id: '12', name: 'entities' },
    positioned: true,
    spans: spans.map((s) => ({
      occurrenceId: s.occurrenceId,
      start: s.start,
      end: s.end,
      quote: '',
      entityType: s.entityType ?? 'organization',
    })),
    counts: { shown: spans.length, total: spans.length, invalid: 0 },
    ...overrides,
  };
}

const DIRTY_LAYER: TextAnnotationLayer = {
  toggleKey: '7:12:entities',
  layerFamily: 'entities',
  producerKind: 'map.ner',
  producerEngine: 'spacy',
  outputColumn: { id: '12', name: 'entities' },
  positioned: false,
  unpositioned: { reason: 'content_hash_mismatch', total: 9 },
};

describe('partitionAnnotationFragments', () => {
  it('covers the whole string exactly once, marks or not', () => {
    const text = 'Boeing Company sued Airbus';
    const fragments = partitionAnnotationFragments(text, [
      mark('a', 0, 14),
      mark('b', 20, 26),
    ]);
    expect(fragments.map((f) => f.text).join('')).toBe(text);
    // Contiguous: each fragment starts where the previous ended.
    fragments.reduce((prevEnd, fragment) => {
      expect(fragment.start).toBe(prevEnd);
      return fragment.end;
    }, 0);
  });

  it('gives an unmarked string one plain fragment', () => {
    const fragments = partitionAnnotationFragments('plain text', []);
    expect(fragments).toEqual([
      { start: 0, end: 10, text: 'plain text', marks: [], owner: null, ownerFirstFragment: false },
    ]);
  });

  it('splits a NESTED pair at every boundary and gives the inner span the click', () => {
    // "Boeing" (0,6) inside "Boeing Company" (0,14) — two spans of ONE family.
    const text = 'Boeing Company sued';
    const fragments = partitionAnnotationFragments(text, [
      mark('outer', 0, 14),
      mark('inner', 0, 6),
    ]);
    expect(fragments.map((f) => [f.text, f.owner?.occurrenceId ?? null])).toEqual([
      ['Boeing', 'inner'],
      [' Company', 'outer'],
      [' sued', null],
    ]);
    // The outer span is still THERE on the inner fragment, with its own full
    // range — the partition loses no coverage, it only picks a click owner.
    expect(fragments[0]!.marks.map((m) => m.occurrenceId)).toEqual(['outer', 'inner']);
    expect(fragments[0]!.marks[0]).toMatchObject({ start: 0, end: 14 });
  });

  it('keeps a split occurrence to ONE tab stop', () => {
    const fragments = partitionAnnotationFragments('Boeing Company sued', [
      mark('outer', 0, 14),
      mark('inner', 7, 14),
    ]);
    const outerFragments = fragments.filter((f) => f.owner?.occurrenceId === 'outer');
    expect(outerFragments).toHaveLength(1);
    expect(outerFragments.every((f) => f.ownerFirstFragment)).toBe(true);

    // A genuinely re-entered owner: outer, inner, outer again.
    const reentered = partitionAnnotationFragments('Boeing Company sued', [
      mark('outer', 0, 14),
      mark('inner', 6, 8),
    ]);
    const owned = reentered.filter((f) => f.owner?.occurrenceId === 'outer');
    expect(owned).toHaveLength(2);
    expect(owned.map((f) => f.ownerFirstFragment)).toEqual([true, false]);
  });

  it('handles crossing (non-nesting) overlaps without dropping either mark', () => {
    const fragments = partitionAnnotationFragments('abcdefghij', [
      mark('a', 0, 5),
      mark('b', 3, 8),
    ]);
    expect(fragments.map((f) => [f.start, f.end, f.owner?.occurrenceId ?? null])).toEqual([
      [0, 3, 'a'],
      [3, 5, 'b'],
      [5, 8, 'b'],
      [8, 10, null],
    ]);
    expect(fragments[1]!.marks.map((m) => m.occurrenceId).sort()).toEqual(['a', 'b']);
  });

  it('drops an out-of-range or empty mark rather than clamping it', () => {
    // A clamped range is a coordinate claim nobody made; the surrounding text
    // must still render.
    const fragments = partitionAnnotationFragments('short', [
      mark('past-end', 2, 99),
      mark('empty', 1, 1),
      mark('inverted', 4, 2),
    ]);
    expect(fragments).toHaveLength(1);
    expect(fragments[0]!.marks).toEqual([]);
    expect(fragments[0]!.text).toBe('short');
  });

  it('indexes in UTF-16 code units, so an astral character shifts nothing', () => {
    // '🧪' is two UTF-16 code units, which is exactly what the server converted
    // its code-point offsets into and what a JS string slices in. If either
    // side used code points, this mark would land one unit early.
    const text = '🧪 Ada Lovelace';
    const fragments = partitionAnnotationFragments(text, [mark('a', 3, 15)]);
    const owned = fragments.find((f) => f.owner !== null);
    expect(owned!.text).toBe('Ada Lovelace');
  });
});

describe('collectMarks', () => {
  it('draws nothing from an UNPOSITIONED layer', () => {
    // Its count still shows in the toggle strip; its stored offsets do not get
    // drawn over text that no longer matches them.
    expect(collectMarks([DIRTY_LAYER], [])).toEqual([]);
  });

  it('omits a layer whose toggle is off', () => {
    const layer = positionedLayer([{ occurrenceId: 's1:1', start: 0, end: 6 }]);
    expect(collectMarks([layer], [])).toHaveLength(1);
    expect(collectMarks([layer], ['7:12:entities'])).toEqual([]);
  });

  it('carries the entity type through to the mark', () => {
    const [only] = collectMarks(
      [positionedLayer([{ occurrenceId: 's1:1', start: 0, end: 6, entityType: 'person' }])],
      [],
    );
    expect(only).toMatchObject({ occurrenceId: 's1:1', entityType: 'person', start: 0, end: 6 });
  });
});

describe('annotationToggles', () => {
  it('reports the drawable count for a positioned layer', () => {
    const [toggle] = annotationToggles(
      [positionedLayer([
        { occurrenceId: 'a', start: 0, end: 6 },
        { occurrenceId: 'b', start: 7, end: 14 },
      ])],
      [],
    );
    expect(toggle).toMatchObject({
      toggleKey: '7:12:entities',
      label: 'Entities',
      enabled: true,
      count: 2,
      positioned: true,
      unpositionedReason: null,
    });
  });

  it('reports an unpositioned layer’s LAST-KNOWN count, with its reason', () => {
    const [toggle] = annotationToggles([DIRTY_LAYER], []);
    expect(toggle).toMatchObject({
      count: 9,
      positioned: false,
      unpositionedReason: 'content_hash_mismatch',
    });
  });

  it('reads the disabled set to decide `enabled`', () => {
    const [toggle] = annotationToggles([positionedLayer([])], ['7:12:entities']);
    expect(toggle.enabled).toBe(false);
  });
});

describe('layerFamilyLabel', () => {
  it('humanizes an unregistered family instead of dropping it', () => {
    expect(layerFamilyLabel('entities')).toBe('Entities');
    expect(layerFamilyLabel('redaction_candidates')).toBe('Redaction candidates');
  });
});
