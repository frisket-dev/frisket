import { useState } from 'react';
import { X } from 'lucide-react';
import { parseCommaLabels } from './formControlHelpers';
import styles from './LabelChipsInput.module.css';

/** A removable-chip tag input with typed array state. Commas remain an input
 * gesture for adding labels, never a storage or request encoding. */
type LabelChipsInputProps = {
  placeholder: string;
  ariaLabel: string;
  testId: string;
} & (
  | { typed: true; value: readonly string[]; onChange: (value: string[]) => void }
  | { typed?: false; value: string; onChange: (value: string) => void }
);

export function LabelChipsInput(props: LabelChipsInputProps) {
  const { value, onChange, placeholder, ariaLabel, testId } = props;
  const typed = props.typed === true;
  const labels = typed ? [...value] : parseCommaLabels(value as string);
  const emit = (next: string[]) => {
    if (typed) (onChange as (value: string[]) => void)(next);
    else (onChange as (value: string) => void)(next.join(', '));
  };
  const [draft, setDraft] = useState('');
  const commit = (raw: string) => {
    const additions = parseCommaLabels(raw);
    if (!additions.length) {
      setDraft('');
      return;
    }
    const seen = new Set(labels.map((label) => label.toLowerCase()));
    const next = [...labels];
    for (const addition of additions) {
      if (!seen.has(addition.toLowerCase())) {
        seen.add(addition.toLowerCase());
        next.push(addition);
      }
    }
    emit(next);
    setDraft('');
  };
  const removeLabel = (label: string) => emit(labels.filter((item) => item !== label));
  return (
    <div className={styles.input} data-testid={testId}>
      {labels.length > 0 && (
        <div className={styles.list}>
          {labels.map((label, index) => (
            <span key={`${label}-${index}`} className={styles.chip} data-testid={`${testId}-chip`}>
              {label}
              <button
                type="button"
                className={styles.remove}
                aria-label={`Remove ${label}`}
                onClick={() => removeLabel(label)}
              >
                <X size={11} />
              </button>
            </span>
          ))}
        </div>
      )}
      <input
        className={`form-input ${styles.draft}`}
        data-testid={`${testId}-draft`}
        aria-label={ariaLabel}
        placeholder={placeholder}
        value={draft}
        onChange={(e) => {
          const next = e.target.value;
          if (next.endsWith(',')) commit(next);
          else setDraft(next);
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            commit(draft);
          } else if (e.key === 'Backspace' && !draft && labels.length) {
            removeLabel(labels[labels.length - 1]);
          }
        }}
        onBlur={() => commit(draft)}
      />
    </div>
  );
}
