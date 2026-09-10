import { describe, expect, it } from 'vitest';

import { transcribeCompareWirePayload } from '../../src/api/transcribeComparePayload';

describe('Transcribe Compare wire payload', () => {
  it('does not synthesize Whisper defaults when an engine projection omits its knobs', () => {
    const payload = transcribeCompareWirePayload({
      engine: 'joint_diarizer_fixture',
    });
    expect(payload).toEqual({
      engine: 'joint_diarizer_fixture',
    });
    // Consent is the server-issued quote token, never an allow_remote flag.
    expect(payload).not.toHaveProperty('allow_remote');
    expect(payload).not.toHaveProperty('language');
    expect(payload).not.toHaveProperty('model_size');
    expect(payload).not.toHaveProperty('vad');
  });

  it('preserves explicitly projected supported values, including false', () => {
    expect(transcribeCompareWirePayload({
      engine: 'faster_whisper',
      language: ' es ',
      model_size: 'large-v3',
      vad: false,
    })).toEqual({
      engine: 'faster_whisper',
      language: 'es',
      model_size: 'large-v3',
      vad: false,
    });
  });

  it('carries the exact duration, options and confirmation without defaults', () => {
    const input = { engine: 'parakeet-tdt', diarize: true, time_limit_seconds: 45, confirmation: 'a'.repeat(64) };
    expect(transcribeCompareWirePayload(input)).toEqual(input);
  });
});
