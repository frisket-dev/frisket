import { describe, expect, it } from 'vitest';

import { alignTranscriptSegments } from '../../src/workbench/transcriptAlign';

describe('alignTranscriptSegments', () => {
  it('keeps speaker attribution visible in aligned transcript text', () => {
    const units = alignTranscriptSegments(
      ['joint', 'plain'],
      {
        joint: [{
          segment_index: 0,
          start: 0,
          end: 1.4,
          speaker: 'S1',
          text: 'Welcome aboard.',
        }],
        plain: [{
          segment_index: 0,
          start: 0,
          end: 1.4,
          text: 'Welcome aboard.',
        }],
      },
      {},
    );

    expect(units).toHaveLength(1);
    expect(units[0].textByEngine).toEqual({
      joint: '[S1] Welcome aboard.',
      plain: 'Welcome aboard.',
    });
  });

  it('does not invent a speaker prefix for a blank speaker label', () => {
    const units = alignTranscriptSegments(
      ['engine'],
      {
        engine: [{
          segment_index: 0,
          start: 3,
          end: 4,
          speaker: '   ',
          text: 'No attributed speaker.',
        }],
      },
      {},
    );

    expect(units[0].textByEngine.engine).toBe('No attributed speaker.');
  });
});
