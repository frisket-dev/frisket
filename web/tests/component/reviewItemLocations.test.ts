import { describe, expect, it } from 'vitest';
import type { ReviewBundleField } from '../../src/api/types';
import { reviewItemLocations } from '../../src/components/review/reviewItemLocations';
import type { ReviewCitationSource } from '../../src/components/review/reviewCitationSources';
import { evidenceArtifact, evidenceSpan } from '../support/evidenceFixtures';

function field(value: unknown): ReviewBundleField {
  return { id: 'entities', runId: '1', sheetId: '1', rowId: '1', columnId: '1', columnName: 'Entities',
    columnType: 'json', semanticType: 'entity_mentions', value: JSON.stringify(value), confidence: null,
    justification: '', reviewState: 'unreviewed', role: 'field', chore: false };
}
function source(spans: ReturnType<typeof evidenceSpan>[]): ReviewCitationSource {
  const artifact = evidenceArtifact({ spans });
  return { id: 'source', artifact, fieldIds: ['entities'], kind: 'TXT', title: 'Transcript', members: [{ fieldId: 'entities', artifact, spans }] };
}

describe('review item locations', () => {
  it('uses stored result indices and keeps multiple matching source locations', () => {
    const value = field([{ text: 'Michigan' }, { text: 'Detroit' }]);
    const sources = [source([
      evidenceSpan({ stable_id: 'second', raw: { item_index: 1 } }),
      evidenceSpan({ stable_id: 'first-a', raw: { item_index: 0 } }),
      evidenceSpan({ stable_id: 'first-b', raw: { item_index: 0 } }),
      evidenceSpan({ stable_id: 'invalid', raw: { item_index: 3 } }),
    ])];
    expect(reviewItemLocations(sources, [value])).toEqual([
      { fieldId: 'entities', index: 1, sourceId: 'source', spanId: 'second' },
      { fieldId: 'entities', index: 0, sourceId: 'source', spanId: 'first-a' },
      { fieldId: 'entities', index: 0, sourceId: 'source', spanId: 'first-b' },
    ]);
  });

  it('uses a list extraction link item index for all of its supporting spans', () => {
    const cited = source([evidenceSpan({ stable_id: 'box' }), evidenceSpan({ stable_id: 'time' })]);
    cited.members[0].itemIndex = 1;
    expect(reviewItemLocations([cited], [field(['first', 'second'])]).map(({ index, spanId }) => ({ index, spanId })))
      .toEqual([{ index: 1, spanId: 'box' }, { index: 1, spanId: 'time' }]);
  });

  it('recovers old NER locations only from exact quote and offsets, never list order', () => {
    const value = field([{ text: 'Michigan', start: 10, end: 18 }]);
    const sources = [source([
      evidenceSpan({ stable_id: 'wrong-occurrence', quote: 'Michigan', selector: { char_start: 0, char_end: 8 } }),
      evidenceSpan({ stable_id: 'correct', quote: 'Michigan', selector: { char_start: 10, char_end: 18 } }),
    ])];
    expect(reviewItemLocations(sources, [value]).map((location) => location.spanId)).toEqual(['correct']);
    expect(reviewItemLocations(sources, [{ ...value, semanticType: null }])).toEqual([]);
  });
});
