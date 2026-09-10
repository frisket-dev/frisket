import type { TranscribeCompareScratchInput } from './types';

/** Build the JSON half of the multipart Transcribe Compare request. Optional
 * keys are presence-sensitive: undefined means the selected engine did not
 * declare the knob and must remain absent (especially VAD — never synthesize
 * the legacy true default for an intrinsic/unsupported engine). */
export function transcribeCompareWirePayload(
  input: TranscribeCompareScratchInput,
): Record<string, unknown> {
  return {
    engine: input.engine,
    ...(input.language !== undefined
      ? { language: input.language?.trim() || null }
      : {}),
    ...(input.model_size !== undefined
      ? { model_size: input.model_size?.trim() || null }
      : {}),
    ...(typeof input.vad === 'boolean' ? { vad: input.vad } : {}),
    ...(typeof input.diarize === 'boolean' ? { diarize: input.diarize } : {}),
    ...(input.time_limit_seconds !== undefined ? { time_limit_seconds: input.time_limit_seconds } : {}),
    ...(input.confirmation !== undefined ? { confirmation: input.confirmation } : {}),
  };
}
