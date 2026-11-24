import { describe, expect, it } from 'vitest';
import { resolveColumnAnnotationPlacement } from '../../src/grid/gridHeaderBand';

describe('resolveColumnAnnotationPlacement', () => {
  const columns = [
    { id: 'source', group: undefined },
    { id: 'answer', group: 'ai-run:7' },
    { id: 'confidence', group: 'ai-run:7' },
    { id: 'notes', group: undefined },
  ];

  it('anchors a same-group annotation to the full group and above its header row', () => {
    expect(resolveColumnAnnotationPlacement(['confidence'], columns, 28)).toEqual({
      first: 1,
      last: 2,
      headerOffset: 28,
    });
  });

  it('keeps ordinary column geometry for ungrouped and mixed-group spans', () => {
    expect(resolveColumnAnnotationPlacement(['source'], columns, 28)).toEqual({
      first: 0,
      last: 0,
      headerOffset: 0,
    });
    expect(resolveColumnAnnotationPlacement(['source', 'answer'], columns, 28)).toEqual({
      first: 0,
      last: 1,
      headerOffset: 0,
    });
  });

  it('ignores hidden annotation members when resolving the visible group', () => {
    expect(resolveColumnAnnotationPlacement(['answer', 'hidden-column'], columns, 28)).toEqual({
      first: 1,
      last: 2,
      headerOffset: 28,
    });
  });
});
