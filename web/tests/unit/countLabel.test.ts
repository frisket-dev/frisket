// frontend-affordance-fixes REAL-BUG: the grid's JSON-array count badge
// (grid/cells.tsx's json cell renderer) and the child-sheet count chip
// (grid/SheetGrid.tsx's childChipLabel) each had their own suffix-only
// pluralization helper, and a column/sheet literally named "people" naively
// pluralized to "2 peoples" — the naive "append 's' unless it already ends in
// s" heuristic doesn't know an irregular plural like "people" is already
// plural. Both now share format.ts's irregular-aware countLabel/pluralizeNoun.

import { describe, expect, it } from 'vitest';
import { countLabel, pluralizeNoun } from '../../src/format';

describe('pluralizeNoun', () => {
  it('singularizes/pluralizes the irregular person/people pair without doubling up', () => {
    expect(pluralizeNoun('people', 2)).toBe('people');
    expect(pluralizeNoun('people', 5)).toBe('people');
    expect(pluralizeNoun('people', 1)).toBe('person');
    expect(pluralizeNoun('person', 1)).toBe('person');
    expect(pluralizeNoun('person', 2)).toBe('people');
  });

  it('preserves the leading letter case of the source noun', () => {
    expect(pluralizeNoun('People', 1)).toBe('Person');
    expect(pluralizeNoun('Person', 3)).toBe('People');
  });

  it('still pluralizes a regular noun with the trailing-s heuristic', () => {
    expect(pluralizeNoun('face', 1)).toBe('face');
    expect(pluralizeNoun('face', 3)).toBe('faces');
    expect(pluralizeNoun('faces', 1)).toBe('face');
    expect(pluralizeNoun('row', 0)).toBe('rows');
    expect(pluralizeNoun('row', 1)).toBe('row');
  });

  // demo-fixes REAL-BUG: a column named `entities` rendered "1 entitie" in the
  // grid's count badge — the singular branch peeled a trailing "s" off any
  // noun at all.
  it('singularizes an -ies plural instead of peeling one letter off it', () => {
    expect(pluralizeNoun('entities', 1)).toBe('entity');
    expect(pluralizeNoun('entities', 2)).toBe('entities');
    expect(pluralizeNoun('entity', 1)).toBe('entity');
    expect(pluralizeNoun('entity', 2)).toBe('entities');
    expect(pluralizeNoun('cities', 1)).toBe('city');
    expect(pluralizeNoun('company', 3)).toBe('companies');
  });

  it('singularizes an -es plural on a sibilant stem', () => {
    expect(pluralizeNoun('addresses', 1)).toBe('address');
    expect(pluralizeNoun('boxes', 1)).toBe('box');
    expect(pluralizeNoun('matches', 1)).toBe('match');
    expect(pluralizeNoun('box', 2)).toBe('boxes');
    expect(pluralizeNoun('match', 2)).toBe('matches');
  });

  it('leaves a singular noun that merely ENDS in s alone rather than inventing one', () => {
    expect(pluralizeNoun('address', 1)).toBe('address');
    expect(pluralizeNoun('status', 1)).toBe('status');
    expect(pluralizeNoun('analysis', 1)).toBe('analysis');
    expect(pluralizeNoun('address', 2)).toBe('addresses');
  });

  // third-review REAL-BUG: the round-trip "verification" (accept a candidate
  // singular when re-pluralizing it reproduces the noun) proves nothing about
  // whether the candidate is a WORD — regularPlural('ga') really is 'gas' —
  // so `countLabel(1, col.name)` on a json column named `gas`/`lens`/`alias`
  // rendered "1 ga" / "1 len" / "1 alia", the same class of bug as
  // "1 entitie". The eight nouns the review found, at the count that used to
  // mangle them:
  it('never mangles a singular noun that happens to end in s', () => {
    for (const noun of ['gas', 'lens', 'bias', 'canvas', 'atlas', 'alias', 'summons']) {
      expect(pluralizeNoun(noun, 1)).toBe(noun);
    }
    // …and each still pluralizes correctly, which is the point of knowing
    // they are singular rather than just refusing to touch them.
    expect(pluralizeNoun('gas', 3)).toBe('gases');
    expect(pluralizeNoun('lens', 3)).toBe('lenses');
    expect(pluralizeNoun('alias', 3)).toBe('aliases');
    expect(pluralizeNoun('summons', 3)).toBe('summonses');
  });

  it('knows the Latin plurals rather than peeling them ("indices" is not "indice")', () => {
    expect(pluralizeNoun('indices', 1)).toBe('index');
    expect(pluralizeNoun('index', 2)).toBe('indices');
    expect(pluralizeNoun('analyses', 1)).toBe('analysis');
    // …which also fixes the plural side: "analysis" pluralized as
    // "analysises" before it was known.
    expect(pluralizeNoun('analysis', 2)).toBe('analyses');
    expect(pluralizeNoun('matrices', 1)).toBe('matrix');
  });

  // The -es reading needs a stem that TAKES "es". Accepting any stem that
  // re-pluralizes turned "cases" into "cas" and "sizes" into "siz".
  it('reads an -es plural as the stem that actually takes -es', () => {
    expect(pluralizeNoun('cases', 1)).toBe('case');
    expect(pluralizeNoun('sizes', 1)).toBe('size');
    expect(pluralizeNoun('phases', 1)).toBe('phase');
    expect(pluralizeNoun('houses', 1)).toBe('house');
    // …while the genuine sibilant stems keep working.
    expect(pluralizeNoun('gases', 1)).toBe('gas');
    expect(pluralizeNoun('aliases', 1)).toBe('alias');
    expect(pluralizeNoun('addresses', 1)).toBe('address');
  });

  // THE GUARANTEE, stated as what the code actually does: a noun is
  // singularized only through the irregular table or a rule whose reading is
  // not a coin-flip, and every other shape comes back EXACTLY as typed. The
  // two ambiguous shapes below are declined for that reason — "1 shoes" is
  // clumsy, "1 shoe"/"1 heroe" would be a guess and an invention.
  it('returns the noun unchanged when no rule accounts for it', () => {
    for (const noun of ['heroes', 'shoes', 'tomatoes', 'kudos', 'corps', 'biceps']) {
      expect(pluralizeNoun(noun, 1)).toBe(noun);
      // …in BOTH directions: the same limit applies to pluralizing. Treating
      // an unexplained plural as a singular produced "heroeses"/"corpses".
      expect(pluralizeNoun(noun, 3)).toBe(noun);
    }
  });

  // "-ies" is both "consonant + y" ("entity") and "-ie" + s ("movie"); the
  // spelling cannot say which, so the common "-ie" nouns are listed rather
  // than minted into "movy".
  it('does not turn an -ie plural into a -y singular', () => {
    expect(pluralizeNoun('movies', 1)).toBe('movie');
    expect(pluralizeNoun('cookies', 1)).toBe('cookie');
    expect(pluralizeNoun('caches', 1)).toBe('cache');
    expect(pluralizeNoun('entities', 1)).toBe('entity');
  });

  it('leaves invariant nouns unchanged in both directions', () => {
    expect(pluralizeNoun('series', 1)).toBe('series');
    expect(pluralizeNoun('series', 4)).toBe('series');
    expect(pluralizeNoun('species', 1)).toBe('species');
    expect(pluralizeNoun('metadata', 2)).toBe('metadata');
  });

  it('handles other small irregulars (child/children)', () => {
    expect(pluralizeNoun('child', 1)).toBe('child');
    expect(pluralizeNoun('child', 2)).toBe('children');
    expect(pluralizeNoun('children', 1)).toBe('child');
    expect(pluralizeNoun('children', 4)).toBe('children');
  });
});

describe('countLabel', () => {
  it('produces "2 people" / "1 person" for the literal reported bug', () => {
    expect(countLabel(2, 'people')).toBe('2 people');
    expect(countLabel(1, 'person')).toBe('1 person');
    expect(countLabel(2, 'person')).toBe('2 people');
  });

  it('still pluralizes a regular noun', () => {
    expect(countLabel(3, 'face')).toBe('3 faces');
    expect(countLabel(1, 'face')).toBe('1 face');
  });

  it('reads the grid entity column correctly at 1 and at N', () => {
    expect(countLabel(1, 'entities')).toBe('1 entity');
    expect(countLabel(2, 'entities')).toBe('2 entities');
  });

  it('falls back to the given default noun when blank', () => {
    expect(countLabel(4, '   ', 'rows')).toBe('4 rows');
    expect(countLabel(1, '', 'items')).toBe('1 item');
  });

  // The Mentions panel's three hand-rolled count labels folded onto this
  // helper, and they grouped their counts — so this one does too rather than
  // printing "12405 rows".
  it('groups the count', () => {
    expect(countLabel(12405, 'row')).toBe('12,405 rows');
  });

  it('renders a json column named for a singular-looking noun without mangling it', () => {
    // grid/cells.tsx feeds this USER-TYPED column names.
    expect(countLabel(1, 'alias')).toBe('1 alias');
    expect(countLabel(1, 'gas')).toBe('1 gas');
    expect(countLabel(1, 'lens')).toBe('1 lens');
    expect(countLabel(1, 'indices')).toBe('1 index');
  });
});
