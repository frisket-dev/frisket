import { PanelSelect } from '../PanelSelect';
import { LabelChipsInput } from './LabelChipsInput';
import { fallbackLanguageLabel } from '../../actions/transcribeEngineCatalog';
import type { LanguageDeclaration } from '../../api/types';

// Catalog-driven language control: renders per the SELECTED
// engine's declaration — nothing (auto_only), a fixed-language note (fixed), an
// Auto-first single picker (single), or an inline multi-select (multi). The
// form stores a single code or a comma-joined list on `params.language`; v1Spec
// converts it to the always-a-list wire param ([] = auto). Generalized across
// transcribe (source of audio speech) and translate (source of text) — the two
// consume the identical declaration shape; only the labels/test-id prefix and
// the fixed-noun differ, so those are props with transcribe-shaped defaults.
export function EngineLanguageControl({
  declaration,
  value,
  onChange,
  label = 'Language',
  autoLabel = 'Auto (detect)',
  testIdPrefix = 'transcribe',
  fixedNoun = 'transcribes',
  multiLabel = 'Expected languages',
}: {
  declaration: LanguageDeclaration | undefined;
  value: string;
  onChange: (next: string) => void;
  label?: string;
  autoLabel?: string;
  testIdPrefix?: string;
  fixedNoun?: string;
  multiLabel?: string;
}) {
  // Absent declaration (a version-skewed/cached catalog entry that lost the
  // `language` field, after the per-field fallback merge still could not supply
  // one): fall back to the SAFE default — a single Auto-first picker — rather
  // than rendering nothing, which would silently drop the source control while
  // other copy still promises detection. A real `auto_only` declaration keeps
  // meaning "no control".
  const decl: LanguageDeclaration =
    declaration ?? { mode: 'single', default: 'auto', choices: null, detects: true, allows_auto: true };
  if (decl.mode === 'auto_only') return null;
  if (decl.mode === 'fixed') {
    const fixedLabel =
      decl.choices?.find((c) => c.value === decl.fixed_language)?.label ??
      (decl.fixed_language ? fallbackLanguageLabel(decl.fixed_language) : '');
    return (
      <p className="form-hint" data-testid={`${testIdPrefix}-language-fixed`}>
        This engine {fixedNoun} {fixedLabel || 'one fixed language'} only.
      </p>
    );
  }
  const choices = decl.choices ?? [];
  if (decl.mode === 'multi') {
    return (
      <div className="param-row">
        <label className="form-label" htmlFor={`${testIdPrefix}-language-chips`}>{multiLabel}</label>
        <LabelChipsInput
          value={value}
          onChange={onChange}
          placeholder="Add language codes…"
          ariaLabel={multiLabel}
          testId={`${testIdPrefix}-language-chips`}
        />
      </div>
    );
  }
  // single: an Auto-first picker, UNLESS the engine cannot auto-detect
  // (allows_auto === false, e.g. Opus-MT) — then no Auto option, and a short
  // hint that an explicit source is required. Empty value with no Auto option
  // selects nothing (the run is gated elsewhere until a language is chosen).
  const allowsAuto = decl.allows_auto !== false;
  const trimmed = value.trim();
  return (
    <div className="param-row">
      <label className="form-label" htmlFor={`${testIdPrefix}-language`}>{label}</label>
      <PanelSelect
        id={`${testIdPrefix}-language`}
        testId={`${testIdPrefix}-language-select`}
        value={trimmed || (allowsAuto ? 'auto' : '')}
        onValueChange={(next) => onChange(next === 'auto' ? '' : next)}
        options={[
          ...(allowsAuto ? [{ value: 'auto', label: autoLabel }] : []),
          ...choices.map((c) => ({ value: c.value, label: c.label })),
        ]}
      />
      {!allowsAuto && (
        <p className="form-hint" data-testid={`${testIdPrefix}-language-required`}>
          This engine needs an explicit source language — it can’t auto-detect.
        </p>
      )}
    </div>
  );
}
