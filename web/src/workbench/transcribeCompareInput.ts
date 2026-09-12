import type { TranscribeCompareScratchInput } from '../api/types';
import type { EngineOption } from '../api/open';
import {
  transcribeDiarizationMode,
  transcribeOptionsForEngine,
} from '../actions/transcribeEngineCatalog';

export const DEFAULT_TRANSCRIBE_MODEL_SIZE = 'base';
export const DEFAULT_TRANSCRIBE_VAD = true;

export interface TranscribeVariantOptions {
  language: string | undefined;
  modelSize: string | undefined;
  vad: boolean | undefined;
  diarize: boolean | undefined;
}

/** Build the presence-sensitive scratch request without discarding an
 * explicitly saved option that the selected target will reject. */
export function transcribeCompareInputFor(
  engine: EngineOption | undefined,
  engineId: string,
  options: TranscribeVariantOptions,
  timeLimit: number,
): TranscribeCompareScratchInput {
  const fields = transcribeFieldsForEngine(engine);
  return {
    engine: engineId,
    time_limit_seconds: timeLimit,
    ...(options.language ? { language: options.language } : fields.language ? { language: null } : {}),
    ...(options.modelSize
      ? { model_size: options.modelSize }
      : fields.modelSize ? { model_size: DEFAULT_TRANSCRIBE_MODEL_SIZE } : {}),
    ...(typeof options.vad === 'boolean'
      ? { vad: options.vad }
      : fields.vad ? { vad: DEFAULT_TRANSCRIBE_VAD } : {}),
    ...(typeof options.diarize === 'boolean'
      ? { diarize: options.diarize }
      : fields.diarizationMode === 'optional' ? { diarize: false } : {}),
  };
}

export function transcribeFieldsForEngine(engine: EngineOption | undefined): {
  language: boolean;
  modelSize: boolean;
  vad: boolean;
  diarizationMode: 'none' | 'optional' | 'intrinsic';
} {
  const support = transcribeOptionsForEngine(engine);
  return {
    language: support.language,
    modelSize: support.model_size,
    vad: support.vad,
    diarizationMode: transcribeDiarizationMode(engine),
  };
}
