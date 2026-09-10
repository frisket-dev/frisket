import { describe, expect, it } from 'vitest';

import {
  differingBoundaryKeys,
  formatTopicUnitTime,
} from '../../src/workbench/topicSegmentationCompareModel';

describe('topicSegmentationCompareModel', () => {
  it('diffs by canonical unit key, treating point versus span as the same boundary', () => {
    expect(
      differingBoundaryKeys(
        [
          { key: 2, kind: 'point', candidate_ids: ['a'] },
          { key: 4, kind: 'span', candidate_ids: ['b'] },
        ],
        [
          { key: 2, kind: 'span', candidate_ids: ['c'] },
          { key: 5, kind: 'point', candidate_ids: ['d'] },
        ],
      ),
    ).toEqual([4, 5]);
  });

  it('formats timed source units without inventing labels for TXT', () => {
    expect(
      formatTopicUnitTime({
        id: 'cue',
        ordinal: 0,
        text: 'x',
        speaker: null,
        start_ms: 61_250,
        end_ms: 62_000,
      }),
    ).toBe('01:01.250–01:02.000');
    expect(
      formatTopicUnitTime({
        id: 'line',
        ordinal: 0,
        text: 'x',
        speaker: null,
        start_ms: null,
        end_ms: null,
      }),
    ).toBeNull();
  });
});
