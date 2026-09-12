import { describe, expect, it } from 'vitest';

import type { EngineOption } from '../../src/api/types';
import { transcribeCompareInputFor } from '../../src/workbench/transcribeCompareInput';

const localParakeet: EngineOption = {
  id: 'parakeet-tdt', label: 'Parakeet', tier: 'local', available: true,
  target_id: 'local-onnx',
  transcription_options: { language: false, vad: true, model_size: false },
  diarization: { supported: true, mode: 'optional', speaker_hint: 'none' },
  targets: [{
    target: 'local-onnx', target_id: 'local-onnx', available: true,
    transcription_options: { language: false, vad: false, model_size: false },
    diarization: { supported: false, mode: 'none' },
  }],
};

describe('Transcribe Compare selected-target inputs', () => {
  it('preserves saved unsupported values for backend refusal instead of retargeting or dropping them', () => {
    expect(transcribeCompareInputFor(localParakeet, 'parakeet-tdt', {
      language: 'es', modelSize: 'small', vad: true, diarize: true,
    }, 60)).toEqual({
      engine: 'parakeet-tdt', time_limit_seconds: 60,
      language: 'es', model_size: 'small', vad: true, diarize: true,
    });
  });
});
