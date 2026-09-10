import { useState } from 'react';
import { SegmentedToggle } from '../PanelPrimitives';
import type { DiarizationDeclaration } from '../../api/types';
import {
  resolveTranscribeDiarizationMode,
  setTranscribeDiarization,
  transcribeDiarizationEnabled,
} from '../../actions/transcribeEngineCatalog';

/** Declaration-driven diarization surface. `optional` renders the existing
 *  request toggle; `intrinsic` renders an always-on informational receipt and
 *  never exposes a knob the engine cannot honor; `none` renders nothing.
 *  Version-skewed declarations without mode retain the shipped supported-bool
 *  behavior (true -> optional, false -> none).
 *
 *  The optional speaker-count input is DECLARATION-DRIVEN, not hard-coded:
 *  `speaker_hint === 'count'` renders an exact-or-min/max control wired to
 *  num_speakers XOR min/max_speakers (mirrors the contract's mutual-exclusion
 *  rule, schemas/media.py `_validate_diarization_against_engine`);
 *  `speaker_hint === 'none'` (Sortformer's released offline checkpoint always
 *  runs its fixed 4-channel pass — no count/range input exists to honor)
 *  renders NO count field. See schemas/media.py `transcribe_speaker_hint`'s
 *  docstring for the one-line flip if the user later wants an advisory
 *  (non-enforced) count field on a "none" engine instead.
 *
 *  Checking the box always shows the S1/S2 labeling hint, plus the engine's
 *  speaker cap when it declares one. */
export function TranscribeDiarizationControl({
  declaration,
  params,
  onParamsChange,
}: {
  declaration: DiarizationDeclaration | undefined;
  params: Record<string, string>;
  onParamsChange: (
    update: Record<string, string> | ((previous: Record<string, string>) => Record<string, string>),
  ) => void;
}) {
  // Exact-vs-range is a UI-only choice of WHICH pair of fields to show, not a
  // wire value itself (num/min/max_speakers are) — local state, seeded from
  // any already-recorded min/max (a reopened saved/proposed spec), so
  // picking "Range" with nothing typed yet doesn't instantly (and wrongly)
  // snap back to "Exact" the moment the derived-from-params read sees no
  // min/max present.
  const [speakerCountMode, setSpeakerCountMode] = useState<'exact' | 'range'>(() => (
    params.min_speakers?.trim() || params.max_speakers?.trim() ? 'range' : 'exact'
  ));
  if (!declaration) return null;
  const mode = resolveTranscribeDiarizationMode(declaration);
  if (mode === 'none') return null;
  if (mode === 'intrinsic') {
    return (
      <div className="form-hint" data-testid="transcribe-diarization-intrinsic">
        <strong>Speaker identification is always on for this engine.</strong>{' '}
        Segments include speaker labels (S1, S2…)
        {declaration.max_speakers
          ? <> — up to {declaration.max_speakers} speakers.</>
          : '.'}
      </div>
    );
  }
  const diarize = transcribeDiarizationEnabled(params, declaration);
  const setSpeakerField = (name: 'num_speakers' | 'min_speakers' | 'max_speakers', raw: string) => {
    const digits = raw.replace(/[^0-9]/g, '');
    onParamsChange((m) => {
      const next = { ...m };
      if (digits) next[name] = digits;
      else delete next[name];
      return next;
    });
  };
  return (
    <>
      <label className="form-check" htmlFor="transcribe-diarize">
        <input
          id="transcribe-diarize"
          type="checkbox"
          data-testid="transcribe-diarize-toggle"
          checked={diarize}
          onChange={(e) => {
            const checked = e.target.checked;
            onParamsChange((m) => setTranscribeDiarization(m, declaration, checked));
          }}
        />
        Identify speakers
      </label>
      {diarize && (
        <>
          {declaration.speaker_hint === 'count' && (
            <div className="param-row" data-testid="transcribe-speaker-count-row">
              <span className="form-label">Speaker count</span>
              <div className="unit-input">
                <SegmentedToggle
                  testId="transcribe-speaker-count-mode"
                  ariaLabel="Speaker count mode"
                  fullWidth={false}
                  value={speakerCountMode}
                  buttonTestId={(v) => `transcribe-speaker-count-mode-${v}`}
                  onValueChange={(next) => {
                    const nextMode = next === 'range' ? 'range' : 'exact';
                    setSpeakerCountMode(nextMode);
                    onParamsChange((m) => {
                      const nm = { ...m };
                      // Exact XOR range (mirrors the contract): switching
                      // modes clears the other shape's fields rather than
                      // leaving them staged to trip the mutual-exclusion
                      // rejection.
                      if (nextMode === 'exact') {
                        delete nm.min_speakers;
                        delete nm.max_speakers;
                      } else {
                        delete nm.num_speakers;
                      }
                      return nm;
                    });
                  }}
                  options={[
                    { value: 'exact', label: 'Exact' },
                    { value: 'range', label: 'Range' },
                  ]}
                />
                {speakerCountMode === 'exact' ? (
                  <input
                    className="form-input"
                    inputMode="numeric"
                    placeholder="Auto"
                    aria-label="Exact speaker count"
                    data-testid="transcribe-num-speakers"
                    value={params.num_speakers ?? ''}
                    onChange={(e) => setSpeakerField('num_speakers', e.target.value)}
                  />
                ) : (
                  <>
                    <input
                      className="form-input"
                      inputMode="numeric"
                      placeholder="Min"
                      aria-label="Minimum speakers"
                      data-testid="transcribe-min-speakers"
                      value={params.min_speakers ?? ''}
                      onChange={(e) => setSpeakerField('min_speakers', e.target.value)}
                    />
                    <span className="unit-input-suffix">to</span>
                    <input
                      className="form-input"
                      inputMode="numeric"
                      placeholder="Max"
                      aria-label="Maximum speakers"
                      data-testid="transcribe-max-speakers"
                      value={params.max_speakers ?? ''}
                      onChange={(e) => setSpeakerField('max_speakers', e.target.value)}
                    />
                  </>
                )}
              </div>
            </div>
          )}
          <p className="form-hint" data-testid="transcribe-diarize-hint">
            Segments gain speaker labels (S1, S2…)
            {declaration.max_speakers ? <> — up to {declaration.max_speakers} speakers.</> : '.'}
          </p>
        </>
      )}
    </>
  );
}
