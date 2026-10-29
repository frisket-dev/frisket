import { describe, expect, it } from 'vitest';
import { draftUsesPdf, sourceReferencesPdf } from '../../src/actions/pdfInput';
import type { GeneratedActionCatalogEntry } from '../../src/api/types';
const columns = [{ name: 'document', type: 'file' }, { name: 'notes', type: 'text' }, { name: 'photo', type: 'image' }];
describe('PDF source selection', () => {
  it.each([['document'], ['notes', 'document'], { text: '{{ notes }}\n{{ document }}' }])('finds selected file references in %j', (source) => {
    expect(sourceReferencesPdf(source, columns)).toBe(true);
  });
  it.each([[], ['notes', 'photo'], { text: 'The document is mentioned, but {{notes}} is the only input.' }])('keeps text/images/empty sources independent (%j)', (source) => {
    expect(sourceReferencesPdf(source, columns)).toBe(false);
  });
  it('requires the catalog to declare a file-capable rich source', () => {
    const entry = { ui_hints: { semantic_controls: { source: 'rich_source' }, source_requirements: [
      { id: 'source', param: 'source', mode: 'columns_or_template', label: 'Source', accepted_column_types: ['text', 'image', 'file'] },
    ] } } as Pick<GeneratedActionCatalogEntry, 'ui_hints'>;
    expect(draftUsesPdf(entry, { source: ['document'] }, columns)).toBe(true);
    entry.ui_hints.source_requirements![0].accepted_column_types = ['text', 'image'];
    expect(draftUsesPdf(entry, { source: ['document'] }, columns)).toBe(false);
  });
});
