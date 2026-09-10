import { describe, expect, it } from 'vitest';

import {
  setTranscribeDiarization,
  transcribeDiarizationEnabled,
} from '../../src/actions/transcribeEngineCatalog';

const MAI_DIARIZATION = {
  supported: true,
  mode: 'optional' as const,
  speaker_hint: 'none' as const,
  default: true,
};

describe('MAI transcription controls', () => {
  it('starts diarization checked and preserves an explicit opt-out', () => {
    const fresh: Record<string, string> = {};
    expect(transcribeDiarizationEnabled(fresh, MAI_DIARIZATION)).toBe(true);
    expect(setTranscribeDiarization(fresh, MAI_DIARIZATION, false)).toEqual({
      diarize: 'false',
    });
  });

  it('removes speaker hints when diarization is disabled', () => {
    expect(setTranscribeDiarization({
      diarize: 'true', num_speakers: '2', min_speakers: '1', max_speakers: '3',
    }, MAI_DIARIZATION, false)).toEqual({ diarize: 'false' });
  });
});
