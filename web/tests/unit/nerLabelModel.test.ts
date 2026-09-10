import { describe, expect, it } from 'vitest';
import {
  nerLabelsOk, normalizeSpacyLabels, selectedSpacyTypes, spacyTypesValue,
} from '../../src/components/action-panel/nerLabelModel';

describe('backend entity aliases in spaCy authoring', () => {
  it('canonicalizes supported aliases and deduplicates equivalent selections', () => {
    const labels = ['ORG', 'People', 'GPE', 'person', 'LOC', ' Work of Art '];
    expect(nerLabelsOk('spacy', labels)).toBe(true);
    expect(normalizeSpacyLabels(labels)).toEqual([
      'organization', 'person', 'location', 'work_of_art',
    ]);
    expect(selectedSpacyTypes(labels.join(', '))).toEqual([
      'person', 'organization', 'location', 'work_of_art',
    ]);
    expect(spacyTypesValue(labels)).toBe('person, organization, location, work_of_art');
  });

  it('retains unknown labels instead of silently broadening or narrowing the filter', () => {
    const labels = ['GPE', 'spaceship', 'street-address'];
    expect(normalizeSpacyLabels(labels)).toEqual(['location', 'spaceship', 'street-address']);
    expect(nerLabelsOk('spacy', labels)).toBe(false);
    expect(nerLabelsOk('spacy', ['work-of-art'])).toBe(false);
    expect(nerLabelsOk('spacy', [])).toBe(false);
    expect(nerLabelsOk('gliner', labels)).toBe(true);
    expect(nerLabelsOk('llm', labels)).toBe(true);
  });
});
