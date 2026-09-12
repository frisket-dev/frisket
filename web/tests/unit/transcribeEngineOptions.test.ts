import { describe, expect, it } from 'vitest';

import type { EngineOption } from '../../src/api/types';
import {
  TRANSCRIBE_ENGINE_FALLBACK,
  findTranscribeEngineDeclaration,
  transcribeDiarizationMode,
  transcribeEnginesFromCatalog,
  transcribeOptionsForEngine,
} from '../../src/actions/transcribeEngineCatalog';

const INTRINSIC_FIXTURE: EngineOption = {
  id: 'joint_diarizer_fixture',
  label: 'Synthetic joint diarizer',
  tier: 'sidecar',
  available: true,
  transcription_options: {
    language: true,
    vad: false,
    model_size: false,
  },
  diarization: {
    supported: true,
    mode: 'intrinsic',
    speaker_hint: 'none',
  },
  target_id: 'fixture-sidecar',
  targets: [{
    target: 'fixture-sidecar',
    target_id: 'fixture-sidecar',
    available: true,
    transcription_options: { language: true, vad: false, model_size: false },
    diarization: { supported: true, mode: 'intrinsic', speaker_hint: 'none' },
  }],
};

describe('transcription engine option declarations', () => {
  it('declares an intrinsic engine without inventing diarize/count/VAD controls', () => {
    expect(transcribeDiarizationMode(INTRINSIC_FIXTURE)).toBe('intrinsic');
    expect(transcribeOptionsForEngine(INTRINSIC_FIXTURE)).toEqual({
      language: true,
      vad: false,
      model_size: false,
    });
  });

  it('retains the supported-bool contract for a declaration with no mode', () => {
    const noMode: EngineOption = {
      ...INTRINSIC_FIXTURE,
      diarization: { supported: true, speaker_hint: 'count' },
      targets: [{ ...INTRINSIC_FIXTURE.targets![0],
        diarization: { supported: true, speaker_hint: 'count' } }],
    };
    expect(transcribeDiarizationMode(noMode)).toBe('optional');
    expect(noMode.diarization?.speaker_hint).toBe('count');
  });

  it('fails closed when a version-skewed catalog has no selected target row', () => {
    const oldWhisperCatalogEntry: EngineOption = {
      id: 'faster_whisper',
      label: 'Old cached Whisper entry',
      tier: 'local',
    };
    expect(transcribeOptionsForEngine(oldWhisperCatalogEntry)).toMatchObject({
      language: false,
      vad: false,
      model_size: false,
    });
  });

  it('keeps the Parakeet fallback auto-only without a language override', () => {
    const parakeet = TRANSCRIBE_ENGINE_FALLBACK.find(
      (engine) => engine.id === 'parakeet-tdt',
    );
    expect(parakeet).toMatchObject({
      label: 'Parakeet (fast multilingual transcription)',
      language: {
        mode: 'auto_only', default: 'auto', detects: false, allows_auto: true,
      },
      transcription_options: { language: false },
    });
    expect(parakeet?.language?.fixed_language).toBeUndefined();
  });

  it('marks a live engine unavailable when the selected target contract is missing', () => {
    const catalog = {
      actions: [{ kind: 'media.transcribe', ui_hints: { engines: [{
        id: 'faster_whisper', label: 'Whisper', tier: 'local', available: true,
        transcription_options: { language: true, vad: true, model_size: true },
      }] } }],
    } as Parameters<typeof transcribeEnginesFromCatalog>[0];
    expect(transcribeEnginesFromCatalog(catalog)[0]).toMatchObject({
      available: false,
      error: 'Option availability could not be checked. Refresh and try again.',
    });
  });

  it('fails closed for an undeclared unknown engine', () => {
    const undeclared: EngineOption = {
      id: 'unknown_fixture',
      label: 'Unknown',
      tier: 'sidecar',
    };
    expect(transcribeOptionsForEngine(undeclared)).toMatchObject({
      language: false,
      vad: false,
      model_size: false,
      context: false,
    });
    expect(transcribeDiarizationMode(undeclared)).toBe('none');
  });

  it('offers context only for engines that declare it', () => {
    const declaring: EngineOption = {
      ...INTRINSIC_FIXTURE,
      transcription_options: {
        ...INTRINSIC_FIXTURE.transcription_options!,
        context: true,
      },
      targets: [{ ...INTRINSIC_FIXTURE.targets![0], transcription_options: {
        ...INTRINSIC_FIXTURE.targets![0].transcription_options!, context: true,
      } }],
    };
    expect(transcribeOptionsForEngine(declaring).context).toBe(true);
    // Declared false — and a declaration MISSING the field entirely (a
    // cached pre-R2 catalog) — both fail closed.
    expect(transcribeOptionsForEngine(INTRINSIC_FIXTURE).context).toBeFalsy();
    expect(transcribeOptionsForEngine({
      ...declaring,
      transcription_options: { ...declaring.transcription_options!, context: false },
      targets: [{ ...declaring.targets![0], transcription_options: {
        ...declaring.targets![0].transcription_options!, context: false,
      } }],
    }).context).toBe(false);
  });

  it('does not unlock fallback context without a selected target row', () => {
    const stale: EngineOption = { id: 'moss', label: 'Old moss entry', tier: 'sidecar' };
    expect(transcribeOptionsForEngine(stale)).toMatchObject({ context: false });
    expect(transcribeOptionsForEngine(
      TRANSCRIBE_ENGINE_FALLBACK.find((engine) => engine.id === 'moss'),
    )).toMatchObject({ context: false });
  });

  it('keeps VibeVoice-ASR unavailable while preserving its intrinsic profile', () => {
    const vibevoice = TRANSCRIBE_ENGINE_FALLBACK.find(
      (engine) => engine.id === 'vibevoice-asr',
    );
    expect(vibevoice).toMatchObject({
      tier: 'sidecar',
      available: false,
      language: { mode: 'auto_only', detects: false, allows_auto: true },
      diarization: { supported: true, mode: 'intrinsic', speaker_hint: 'none' },
      transcription_options: {
        language: false,
        vad: false,
        model_size: false,
        context: true,
      },
    });
    expect(transcribeDiarizationMode(vibevoice)).toBe('none');
  });
});

describe('dead engine names and alias resolution', () => {
  it('resolves only the advertised alias; dead names find nothing', () => {
    // The shipped alias union carries only advertised aliases, so a dead name resolves no
    // declaration here (the backend rejects it at validation, naming the
    // canonical replacement).
    for (const dead of [
      'local',
      'sidecar',
      'quality',
      'faster-whisper',
      'modal',
      'parakeet',
      'parakeet_modal',
      'remote',
    ]) {
      expect(
        findTranscribeEngineDeclaration(TRANSCRIBE_ENGINE_FALLBACK, dead),
      ).toBeUndefined();
    }
    // The advertised alias keeps resolving.
    expect(
      findTranscribeEngineDeclaration(TRANSCRIBE_ENGINE_FALLBACK, 'whisper')?.id,
    ).toBe('faster_whisper');
  });

  it('carries no remote row in the fallback', () => {
    // The `remote` placement row is deleted; the
    // provider-qualified openai/whisper-1 is the authorable form.
    expect(
      TRANSCRIBE_ENGINE_FALLBACK.find((engine) => engine.id === 'remote'),
    ).toBeUndefined();
    expect(
      TRANSCRIBE_ENGINE_FALLBACK.map((engine) => engine.id),
    ).toContain('openai/whisper-1');
  });

  it('offers openai/whisper-1 as the authorable hosted transcription id', () => {
    const whisper1 = TRANSCRIBE_ENGINE_FALLBACK.find(
      (engine) => engine.id === 'openai/whisper-1',
    );
    expect(whisper1).toBeDefined();
    expect(whisper1?.tier).toBe('hosted');
    expect(whisper1?.billable).toBe(true);
    expect(whisper1?.available).toBe(false);
    // Declaration lookup by the literal provider/model id works (no alias
    // rewriting applies to a "/" id).
    expect(
      findTranscribeEngineDeclaration(TRANSCRIBE_ENGINE_FALLBACK, 'openai/whisper-1')?.id,
    ).toBe('openai/whisper-1');
  });
});
