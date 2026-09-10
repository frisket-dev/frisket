// parakeet_modal advertises diarization.supported=true for Sortformer. The
// declaration must thread through the catalog resolver so the form can render a
// diarize control + the 4-speaker cap warning; every other engine stays
// unsupported. (The Python catalog hint is action_catalog_hints.py
// `_transcribe_diarization_hint`; this pins the TS consumption of that shape.)

import { describe, expect, it } from 'vitest';

import type {
  ActionCatalogPayload,
  DiarizationDeclaration,
  EngineOption,
} from '../../src/api/types';
import {
  TRANSCRIBE_ENGINE_FALLBACK,
  transcribeEnginesFromCatalog,
} from '../../src/actions/transcribeEngineCatalog';

function catalogWith(engines: EngineOption[]): ActionCatalogPayload {
  return {
    schema_version: 'frisket.action_catalog.v2',
    actions: [
      {
        kind: 'media.transcribe',
        ui_hints: { engines },
      } as ActionCatalogPayload['actions'][number],
    ],
    action_schema: {},
    error_schema: {},
    result_schema: {},
    receipt_schema: {},
    validation_result_schema: {},
  };
}

const SORTFORMER: DiarizationDeclaration = { supported: true, max_speakers: 4, speaker_hint: 'none' };

describe('transcribe diarization declaration', () => {
  it('surfaces parakeet_modal diarization support + speaker cap from the catalog', () => {
    const engines = transcribeEnginesFromCatalog(
      catalogWith([
        {
          id: 'parakeet_modal',
          label: 'Parakeet on Modal',
          local: false,
          source: 'modal',
          available: true,
          diarization: SORTFORMER,
        },
        {
          id: 'faster_whisper',
          label: 'Whisper',
          local: true,
          source: 'local',
          available: true,
          diarization: { supported: false },
        },
      ]),
    );
    const byId = Object.fromEntries(engines.map((e) => [e.id, e]));
    expect(byId.parakeet_modal.diarization?.supported).toBe(true);
    expect(byId.parakeet_modal.diarization?.max_speakers).toBe(4);
    // a non-diarizing engine stays an honest "no" (no lying control)
    expect(byId.faster_whisper.diarization?.supported).toBe(false);
  });

  it('pre-catalog fallback keeps diarization claims honest', () => {
    // The fallback renders before the live catalog loads. Engines whose
    // fallback declares diarization (moss intrinsic; the collapsed
    // parakeet-tdt's Sortformer Modal build) must carry the same
    // contract as the live catalog — catalog failure must not change
    // apparent engine semantics. Gateway/hosted-gated engines (moss) stay
    // unavailable so nothing can run on an unconfirmed promise;
    // parakeet-tdt is available pre-catalog because its PREFERRED target is
    // local — a diarize=true request routes to Modal at RESOLUTION, where
    // the server's per-target support + claims gate enforce honestly.
    for (const engine of TRANSCRIBE_ENGINE_FALLBACK) {
      if (engine.diarization?.supported) {
        expect(['optional', 'intrinsic']).toContain(engine.diarization.mode);
        if (engine.id !== 'parakeet-tdt') {
          expect(engine.available).toBe(false);
        }
      } else {
        expect(engine.diarization?.supported ?? false).toBe(false);
      }
    }
    // The collapsed engine's fallback matches the engine-level union the
    // backend catalog serves (Sortformer cap 4, no count hint).
    const parakeet = TRANSCRIBE_ENGINE_FALLBACK.find((e) => e.id === 'parakeet-tdt');
    expect(parakeet?.diarization).toEqual({
      supported: true,
      mode: 'optional',
      max_speakers: 4,
      speaker_hint: 'none',
    });
  });

  // DECLARATION-DRIVEN speaker-count control (diarization-UI slice):
  // Sortformer's released offline checkpoint always runs its fixed 4-channel
  // pass with no count/range input, so the catalog declares speaker_hint
  // "none" — the form must render NO count field for it (this is pinned at
  // the component level in ActionFormTranscribeDiarization.test.tsx; this
  // test pins the TS-consumed shape carrying that value through).
  it('carries speaker_hint "none" through the catalog resolver for parakeet_modal', () => {
    const engines = transcribeEnginesFromCatalog(
      catalogWith([
        {
          id: 'parakeet_modal',
          label: 'Parakeet on Modal',
          local: false,
          source: 'modal',
          available: true,
          diarization: SORTFORMER,
        },
      ]),
    );
    expect(engines[0].diarization?.speaker_hint).toBe('none');
  });
});
