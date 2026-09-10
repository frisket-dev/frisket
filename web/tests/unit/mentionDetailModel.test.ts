// Which mention a clicked mark belongs to, and the `entity_eq` payload it
// corresponds to. Small, but it is where the two identity modes (D6) are
// decided, and getting it wrong silently narrows or widens what the panel
// claims.

import { describe, expect, it } from 'vitest';

import {
  mentionDetailFilterValue,
  mentionDetailKey,
  mentionTargetForMark,
  snippetParts,
} from '../../src/workbench/mentionDetailModel';
import type { EntityMentionOccurrence } from '../../src/api/open';
import type { AnnotationMark } from '../../src/workbench/textAnnotationModel';



function mark(overrides: Partial<AnnotationMark> = {}): AnnotationMark {
  return {
    occurrenceId: '1:1',
    toggleKey: '7:12:entities',
    layerFamily: 'entities',
    entityType: 'person',
    entityFingerprint: 'ada lovelace',
    entityColumnId: '12',
    start: 0,
    end: 11,
    quote: 'A. Lovelace',
    ...overrides,
  };
}

describe('mentionTargetForMark', () => {
  it('carries the fingerprint, so clicking one spelling opens the GROUP', () => {
    expect(mentionTargetForMark(mark(), '7')).toEqual({
      sheetId: '7',
      columnId: '12',
      type: 'person',
      fingerprint: 'ada lovelace',
      label: 'A. Lovelace',
    });
  });

  it('falls back to the exact spelling when there is no fingerprint', () => {
    // Correct for the unfingerprinted types, and for anything else it degrades
    // to the narrower still-true group rather than to nothing.
    const target = mentionTargetForMark(
      mark({ entityType: 'money', entityFingerprint: null, quote: '$1,250,000' }),
      '7',
    );
    expect(target).toMatchObject({ type: 'money', fingerprint: null, label: '$1,250,000' });
  });

  it('refuses a mark with no type at all', () => {
    // Type is half the identity in BOTH modes; without it there is no group to
    // ask about, and guessing one would be an invented claim.
    expect(mentionTargetForMark(mark({ entityType: null }), '7')).toBeNull();
  });
});

describe('mentionDetailFilterValue', () => {
  it('emits the fingerprint selector for a fingerprinted mention', () => {
    expect(mentionDetailFilterValue(mentionTargetForMark(mark(), '7')!)).toEqual({
      type: 'person',
      fingerprint: 'ada lovelace',
    });
  });

  it('emits the exact-spelling selector otherwise', () => {
    const target = mentionTargetForMark(
      mark({ entityType: 'date', entityFingerprint: null, quote: 'March 2024' }),
      '7',
    );
    expect(mentionDetailFilterValue(target!)).toEqual({ type: 'date', text: 'March 2024' });
  });
});

describe('mentionDetailKey', () => {
  it('distinguishes two spellings of one group from the group itself', () => {
    const grouped = mentionDetailKey({
      sheetId: '7', columnId: '12', type: 'person', fingerprint: 'ada lovelace', label: 'Ada',
    });
    const spelling = mentionDetailKey({
      sheetId: '7', columnId: '12', type: 'person', fingerprint: null, label: 'Ada',
    });
    expect(grouped).not.toBe(spelling);
    // Two spellings of ONE fingerprint group are one fetch, not two.
    expect(grouped).toBe(mentionDetailKey({
      sheetId: '7', columnId: '12', type: 'person', fingerprint: 'ada lovelace', label: 'A. Lovelace',
    }));
  });
});

describe('snippetParts', () => {
  const occurrence = (overrides: Partial<EntityMentionOccurrence['snippet']> = {}) => ({
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
      ...overrides,
    },
  });

  it('cuts at the server’s UTF-16 offsets rather than searching for the quote', () => {
    // The snippet mentions "Lovelace" twice; a search-based renderer marks the
    // wrong one, which is the whole reason offsets exist.
    const parts = snippetParts(
      occurrence({
        text: 'Lovelace wrote; Ada Lovelace signed',
        markStart: 16,
        markEnd: 28,
      }),
    );
    expect(parts.mark).toBe('Ada Lovelace');
    expect(parts.before).toBe('Lovelace wrote; ');
    expect(parts.after).toBe(' signed');
  });

  it('carries the server’s truncation flags rather than assuming an ellipsis', () => {
    const whole = snippetParts(
      occurrence({ truncatedStart: false, truncatedEnd: false }),
    );
    expect(whole.truncatedStart).toBe(false);
    expect(whole.truncatedEnd).toBe(false);
  });

  it('degrades to unmarked text rather than clamping an impossible range', () => {
    // A clamped highlight is a coordinate claim nobody made; showing the
    // context with no mark is the honest fallback.
    const parts = snippetParts(occurrence({ markStart: 12, markEnd: 999 }));
    expect(parts.mark).toBe('');
    expect(parts.before).toBe('convened by Ada Lovelace to review');
    expect(parts.after).toBe('');
  });
});
