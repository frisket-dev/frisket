import { describe, expect, it } from 'vitest';
import type { ColumnDef } from '../../src/api/types';
import { dedupeDefaultColumnName } from '../../src/components/action-panel/formControlHelpers';

const columns = (...names: string[]): ColumnDef[] => names.map((name, index) => ({
  id: String(index), name, type: 'text',
}));

describe('independently named typed outputs', () => {
  it('dedupes each transcription output without reserving implicit siblings', () => {
    const existing = columns('transcript', 'transcript_2_segments', 'detected_language');
    expect(dedupeDefaultColumnName('transcript', existing)).toBe('transcript_2');
    expect(dedupeDefaultColumnName('transcript_segments', existing)).toBe('transcript_segments');
    expect(dedupeDefaultColumnName('detected_language', existing)).toBe('detected_language_2');
  });

  it('retains trimmed case-insensitive collision detection for every output', () => {
    expect(dedupeDefaultColumnName('ocr_text', columns(' OCR_TEXT ', 'ocr_text_2')))
      .toBe('ocr_text_3');
    expect(dedupeDefaultColumnName('ocr_blocks', columns('ocr_text'))).toBe('ocr_blocks');
  });
});
