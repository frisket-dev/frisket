import { describe, expect, it } from 'vitest';
import { createReviewSources, displayStarts, pageAddress, reviewedStarts, reviewQueues } from '../../src/components/importWorkspace/pdfPacket/reviewSources';

const sources = [
  { key: 'first', filename: 'first.pdf', page_start: 1, page_end: 3, display_start: 1 },
  { key: 'second', filename: 'second.pdf', page_start: 4, page_end: 8, display_start: 4 },
];

describe('combined PDF review', () => {
  it('maps display pages to source occurrences and original page numbers', () => {
    const map = createReviewSources(sources);
    expect(pageAddress(map, 3)).toMatchObject({ key: 'first', localPage: 3, sourcePage: 3 });
    expect(pageAddress(map, 4)).toMatchObject({ key: 'second', localPage: 4, sourcePage: 1 });
    expect(pageAddress(map, 8)).toMatchObject({ key: 'second', localPage: 8, sourcePage: 5 });
    expect(pageAddress(map, 9)).toBeNull();
  });

  it('retains immutable file starts and separates occurrence decisions', () => {
    const map = createReviewSources(sources);
    expect(reviewedStarts(map, [2, 6])).toEqual({ first: [1, 2], second: [4, 6] });
    expect(reviewedStarts(map, [1, 4, 4])).toEqual({ first: [1], second: [4] });
    expect(displayStarts(map, { first: [1, 2], second: [4, 6] })).toEqual([1, 2, 4, 6]);
    expect(() => displayStarts(map, { second: [3] })).toThrow(/outside/);
  });

  it('never queues fixed boundaries or already answered pages', () => {
    expect(reviewQueues({ suggested_pages: [1, 4, 5, 6], unsure_pages: [4, 6, 7] }, [1, 4], [5], [7])).toEqual({ suggested: [6], unsure: [6] });
  });
});
