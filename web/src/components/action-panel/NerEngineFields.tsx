import '../PanelPrimitives.css';
import { LabelChipsInput } from './LabelChipsInput';
import { SpacyTypeMultiSelect } from './NerFieldControls';
import styles from './NerEngineFields.module.css';

interface NerEngineFieldsProps {
  engine: string;
  labels: readonly string[];
  labelsError?: string | null;
  onLabelsChange: (labels: string[]) => void;
}

/** Engine-specific NER authoring controls. Label state remains owned by the
 *  action form so switching engines cannot discard the user's schema. */
export function NerEngineFields({
  engine,
  labels,
  labelsError,
  onLabelsChange,
}: NerEngineFieldsProps) {
  return (
    <>
      <div className={styles.fields} data-testid="ner-engine-fields">
        {engine === 'spacy' && (
          <>
            <div className="form-label">Entity types</div>
            <p className="form-hint">
              spaCy has a fixed set of entity types — pick the ones to extract. Unchecked
              types are never extracted, stored, or written as spans.
            </p>
            <SpacyTypeMultiSelect
              value={labels}
              onChange={onLabelsChange}
              testId="ner-spacy-types"
              error={labelsError}
            />
          </>
        )}
        {(engine === 'gliner' || engine === 'llm') && (
          <>
            <div className="form-label">{engine === 'gliner' ? 'Labels' : 'Entity schema'}</div>
            <p className="form-hint">
              {engine === 'gliner'
                ? 'GLiNER is zero-shot — type any label and press Enter or comma to add it.'
                : 'LLM extraction uses these labels as the structured entity types.'}
            </p>
            <LabelChipsInput
              key={engine}
              typed
              value={labels}
              onChange={onLabelsChange}
              placeholder="e.g. person, organization, ship name"
              ariaLabel={engine === 'gliner' ? 'Entity labels' : 'LLM entity labels'}
              testId={`ner-${engine}-labels`}
            />
            {labelsError && (
              <p className="panel-error" data-testid={`ner-${engine}-labels-error`} role="alert">
                {labelsError}
              </p>
            )}
          </>
        )}
      </div>
    </>
  );
}
