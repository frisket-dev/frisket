// THE shared TRANSCRIBE engine-catalog source (the same no-drift rule the OCR
// bake-off established): the transcribe ACTION form (single-pick engine select in the drawer) and the
// Transcribe Compare tab (multi-chip bake-off, held pending the shell
// extraction) both render the option set this module resolves. The two
// surfaces may differ in INTERACTION (pick-one vs add-many) but never in the
// option set, labels, local/remote metadata, availability, or cost hints —
// all of that lives here.
//
// Runtime truth is the backend action catalog (media.transcribe
// ui_hints.engines); TRANSCRIBE_ENGINE_FALLBACK is the honest pre-catalog
// placeholder both surfaces share when the catalog has not loaded or failed
// (remote/modal engines are marked unavailable there so nothing can run on
// them without live catalog truth).
//
// SIBLING MODULE, not an addition to actions/engineCatalog.ts: this module
// UNIONS engineCatalog.ts's already engine-kind-agnostic helpers
// (tierForEngine / engineTierLabel / engineTierOptions / engineIsRemote /
// engineModelsSummary / makeEnginesFromCatalog) instead of duplicating them, and
// re-exports them so transcribe consumers have one import surface. The resolver
// itself is built from engineCatalog.ts's `makeEnginesFromCatalog` factory.

import type {
  DiarizationDeclaration,
  EngineOption,
  LanguageDeclaration,
  TranscriptionOptionDeclaration,
} from '../api/types';
import { makeEnginesFromCatalog } from './engineCatalog';
import transcribeBuiltinDeclarations from '../assets/transcribe_builtin_declarations.json' with { type: 'json' };

// Whisper-family single-language picker source (openai/whisper tokenizer set,
// mirrors src/frisket/contracts/actions/schemas/media.py WHISPER_LANGUAGES).
// The pre-catalog fallback carries a small, common subset; live catalog truth
// (ui_hints.engines[*].language) supplies the full ~99-language list.
const WHISPER_FALLBACK_CHOICES = [
  ['ar', 'Arabic'], ['zh', 'Chinese'], ['nl', 'Dutch'], ['en', 'English'],
  ['fr', 'French'], ['de', 'German'], ['hi', 'Hindi'], ['it', 'Italian'],
  ['ja', 'Japanese'], ['ko', 'Korean'], ['pt', 'Portuguese'], ['ru', 'Russian'],
  ['es', 'Spanish'], ['uk', 'Ukrainian'],
].map(([value, label]) => ({ value, label }));

const FALLBACK_LANGUAGE_NAMES: Record<string, string> = Object.fromEntries(
  WHISPER_FALLBACK_CHOICES.map((choice) => [choice.value, choice.label]),
);

/** English name for a common ISO code, else the code itself. Used for the
 *  fixed-language note when the declaration carries no `choices` to map from. */
export function fallbackLanguageLabel(code: string): string {
  return FALLBACK_LANGUAGE_NAMES[code] ?? code;
}

const WHISPER_SINGLE: LanguageDeclaration = {
  mode: 'single',
  default: 'auto',
  choices: WHISPER_FALLBACK_CHOICES,
  detects: true,
};
const PARAKEET_FIXED_EN: LanguageDeclaration = {
  mode: 'fixed',
  default: 'en',
  fixed_language: 'en',
  choices: null,
  detects: false,
};

const NO_TRANSCRIPTION_OPTIONS: TranscriptionOptionDeclaration = {
  language: false,
  vad: false,
  model_size: false,
  context: false,
  clean: false,
};

type TranscribeBuiltinEngineId = keyof typeof transcribeBuiltinDeclarations.engines;

/** Stable built-in option/alias declarations come from a backend-owned artifact
 * checked against TRANSCRIBE_ENGINE_TABLE by the Python suite. Runtime catalog
 * fields still win; this artifact only keeps the pre-catalog fallback honest. */
function builtinTranscriptionOptions(
  engineId: TranscribeBuiltinEngineId,
): TranscriptionOptionDeclaration {
  return transcribeBuiltinDeclarations.engines[engineId].transcription_options;
}

export {
  tierForEngine,
  engineTierLabel,
  engineTierOptions,
  engineIsRemote,
  engineModelsSummary,
  mergeEnginesWithFallback,
} from './engineCatalog';

export const TRANSCRIBE_ENGINE_FALLBACK: EngineOption[] = [
  // Post-R4 roster: the R2/R3b retired spellings (`parakeet`,
  // `parakeet_modal`, `modal`, `faster-whisper`, the venue/tier words) and
  // the deprecated `remote` row were DELETED — the backend rejects them at
  // validation with the canonical replacement named. Labels are
  // venue-neutral friendly copy — the venue truth (where a run executes,
  // its cost/egress) lives in the run's route-computed claims, not in
  // engine labels.
  {
    id: 'faster_whisper',
    label: 'Whisper (multilingual transcription)',
    tier: 'local',
    available: false,
    error: 'Action catalog unavailable',
    language: WHISPER_SINGLE,
    transcription_options: builtinTranscriptionOptions('faster_whisper'),
  },
  {
    id: 'parakeet-tdt',
    label: 'Parakeet (fast English transcription)',
    tier: 'local',
    // Unavailable pre-catalog (the moss precedent): the local build needs
    // downloaded artifacts and the catalog's probe is the only honest
    // availability source — the fallback never pretends readiness.
    available: false,
    error: 'Action catalog unavailable',
    language: PARAKEET_FIXED_EN,
    // Engine-level union: the Modal-served build diarizes (Sortformer,
    // cap 4); per-target enforcement happens at resolution, and the local
    // build's inability surfaces as an honest refusal, never a silent drop.
    diarization: { supported: true, mode: 'optional', max_speakers: 4, speaker_hint: 'none' },
    transcription_options: builtinTranscriptionOptions('parakeet-tdt'),
  },
  {
    // Sidecar-gated, marked unavailable pre-catalog: availability comes
    // from the gateway's versioned-worker probe, so the fallback never
    // pretends a GPU worker is reachable.
    id: 'moss',
    label: 'MOSS diarizing transcription (via models gateway)',
    tier: 'sidecar',
    available: false,
    error: 'Action catalog unavailable',
    language: { mode: 'auto_only', default: 'auto', detects: false, allows_auto: true },
    // The fallback must carry the same diarization contract as the live
    // catalog — catalog failure must not change apparent engine semantics.
    diarization: { supported: true, mode: 'intrinsic', speaker_hint: 'none' },
    transcription_options: builtinTranscriptionOptions('moss'),
  },
  {
    // Fixed sidecar profile: intrinsic diarization, timestamped segments,
    // automatic unreported language, and a free-text hotword context only.
    id: 'vibevoice-asr',
    label: 'VibeVoice-ASR diarizing transcription (via models gateway)',
    tier: 'sidecar',
    available: false,
    error: 'Action catalog unavailable',
    language: { mode: 'auto_only', default: 'auto', detects: false, allows_auto: true },
    diarization: { supported: true, mode: 'intrinsic', speaker_hint: 'none' },
    transcription_options: builtinTranscriptionOptions('vibevoice-asr'),
  },
  {
    // The authorable provider-qualified transcription id. The deprecated
    // 'remote' placement row is not accepted. This is a
    // free-form provider/model engine, so it has NO row in
    // transcribe_builtin_declarations.json (that artifact mirrors
    // TRANSCRIBE_ENGINE_TABLE only) — its options are inlined here and match
    // the backend's remote-transport capability family (whisper-class:
    // language hint only). Hosted + billable, so honestly unavailable until
    // the live catalog loads, like every hosted fallback entry.
    id: 'openai/whisper-1',
    label: 'OpenAI Whisper (remote provider API)',
    tier: 'hosted',
    billable: true,
    available: false,
    error: 'Action catalog unavailable',
    language: WHISPER_SINGLE,
    transcription_options: {
      language: true,
      vad: false,
      model_size: false,
      context: false,
      clean: false,
    },
  },
  {
    id: 'openrouter/microsoft/mai-transcribe-2',
    label: 'Microsoft MAI-Transcribe 2 (OpenRouter)',
    tier: 'hosted',
    billable: true,
    available: false,
    error: 'Action catalog unavailable',
    language: WHISPER_SINGLE,
    diarization: { supported: true, mode: 'optional', speaker_hint: 'none', default: true },
    transcription_options: builtinTranscriptionOptions('openrouter/microsoft/mai-transcribe-2'),
  },
];

// Every shipped alias is advertised. A dead name finds nothing here: the
// backend rejects it at validation with the canonical replacement named.
const TRANSCRIBE_SHIPPED_ALIASES: Readonly<Record<string, string>> = Object.fromEntries(
  Object.entries(transcribeBuiltinDeclarations.engines).flatMap(
    ([canonicalId, declaration]) =>
      declaration.aliases.map((alias) => [alias, canonicalId] as const),
  ),
);

/** Resolve shipped backend aliases while leaving direct provider/model ids
 * fail-closed unless the live catalog declares them. The EXACT id wins
 * before alias canonicalization (mirrors the backend's raw-before-canonical
 * rule): a version-skewed live catalog may advertise an id this
 * client does not ship — its own declaration must keep winning. */
export function findTranscribeEngineDeclaration(
  engines: EngineOption[],
  engineId: string,
): EngineOption | undefined {
  const direct = engines.find((candidate) => candidate.id === engineId);
  if (direct) return direct;
  const canonicalId = TRANSCRIBE_SHIPPED_ALIASES[engineId] ?? engineId;
  return engines.find((candidate) => candidate.id === canonicalId);
}

export type TranscriptionDiarizationMode = 'none' | 'optional' | 'intrinsic';

/** Resolve the additive diarization enum while retaining the shipped
 * supported-bool contract for version-skewed declarations. */
export function resolveTranscribeDiarizationMode(
  declaration: DiarizationDeclaration | undefined,
): TranscriptionDiarizationMode {
  return declaration?.mode ?? (declaration?.supported ? 'optional' : 'none');
}

/** Resolve an omitted optional toggle through the engine's declared default. */
export function transcribeDiarizationEnabled(
  params: Record<string, string>,
  declaration: DiarizationDeclaration | undefined,
): boolean {
  return params.diarize === 'true'
    || (params.diarize === undefined && declaration?.default === true);
}

/** Preserve an explicit opt-out when an engine defaults diarization on. */
export function setTranscribeDiarization(
  params: Record<string, string>,
  declaration: DiarizationDeclaration | undefined,
  enabled: boolean,
): Record<string, string> {
  const next = { ...params };
  if (enabled) {
    next.diarize = 'true';
    return next;
  }
  if (declaration?.default === true) next.diarize = 'false';
  else delete next.diarize;
  delete next.num_speakers;
  delete next.min_speakers;
  delete next.max_speakers;
  return next;
}

export function transcribeDiarizationMode(
  engine: EngineOption | undefined,
): TranscriptionDiarizationMode {
  return resolveTranscribeDiarizationMode(engine?.diarization);
}

/** Resolve per-knob support. Live declarations win; a matching built-in uses
 * its explicit pre-catalog fallback for old cached catalogs, while an unknown
 * engine with no declaration fails closed instead of inheriting Whisper. */
export function transcribeOptionsForEngine(
  engine: EngineOption | undefined,
): TranscriptionOptionDeclaration {
  if (engine?.transcription_options) return engine.transcription_options;
  const fallback = engine
    ? TRANSCRIBE_ENGINE_FALLBACK.find((candidate) => candidate.id === engine.id)
    : undefined;
  return fallback?.transcription_options ?? NO_TRANSCRIPTION_OPTIONS;
}

/** Resolve the transcribe engine option set from a loaded action catalog.
 *  Returns the shared fallback when the catalog is absent or has no hints. */
export const transcribeEnginesFromCatalog = makeEnginesFromCatalog(
  'media.transcribe',
  TRANSCRIBE_ENGINE_FALLBACK,
);
