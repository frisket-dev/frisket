import {
  RECOMMENDED_SPACY_TYPES,
  SPACY_CANONICAL_TYPE_VALUES,
  SPACY_TYPE_TIERS,
} from './nerLabelModel';
import styles from './NerFieldControls.module.css';

/** A toggle-chip multi-select over spaCy's fixed entity schema, rendered as
 *  the 17 CANONICAL types with readable names (nerLabelModel.ts) rather than
 *  the 18 raw OntoNotes tags — unlike GLiNER's free-text chips
 *  (LabelChipsInput), spaCy's schema is fixed, so `labels` here filters the
 *  built-in types rather than naming arbitrary ones.
 *
 *  An EMPTY selection is invalid, not "all types": the op used to
 *  silently substitute a hidden person/organization/location default for it,
 *  and that fallback is gone. `error` renders the blocking message; Run is
 *  gated by the caller on the same `nerLabelsOk` predicate. */
export function SpacyTypeMultiSelect({
  value,
  onChange,
  testId,
  error,
}: {
  value: readonly string[];
  onChange: (value: string[]) => void;
  testId: string;
  error?: string | null;
}) {
  const selected = new Set(value);
  const toggle = (type: string) => {
    const next = new Set(selected);
    if (next.has(type)) next.delete(type);
    else next.add(type);
    onChange(SPACY_CANONICAL_TYPE_VALUES.filter((type) => next.has(type)));
  };
  return (
    <div role="group" aria-label="Entity types" data-testid={testId}>
      {SPACY_TYPE_TIERS.map((tier) => (
        <div
          key={tier.id}
          className={`${styles.tier}${tier.id === 'noise' ? ` ${styles.isNoise}` : ''}`}
          data-tier={tier.id}
        >
          <p className={styles.tierLabel} id={`${testId}-tier-${tier.id}`}>
            {tier.caption}
          </p>
          <div
            className={styles.chips}
            role="group"
            aria-labelledby={`${testId}-tier-${tier.id}`}
          >
            {tier.options.map((option) => (
              <button
                key={option.value}
                type="button"
                className={`${styles.chip}${selected.has(option.value) ? ` ${styles.active}` : ''}`}
                data-testid={`${testId}-${option.value}`}
                aria-pressed={selected.has(option.value)}
                onClick={() => toggle(option.value)}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>
      ))}
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.action}
          data-testid={`${testId}-select-all`}
          onClick={() => onChange([...SPACY_CANONICAL_TYPE_VALUES])}
        >
          Select all
        </button>
        <button
          type="button"
          className={styles.action}
          data-testid={`${testId}-reset-recommended`}
          onClick={() => onChange([...RECOMMENDED_SPACY_TYPES])}
        >
          Reset recommended
        </button>
      </div>
      {error && (
        <p className="form-error" data-testid={`${testId}-error`} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
