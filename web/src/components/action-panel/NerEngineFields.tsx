import type { DownloadableArtifact } from '../../api/open';
import { PinnedArtifactDownload } from '../PinnedArtifactDownload';
import { LabelChipsInput } from './LabelChipsInput';
import { SpacyTypeMultiSelect } from './NerFieldControls';
import styles from './NerEngineFields.module.css';

interface SpacyDownload {
  artifact: DownloadableArtifact;
  onInstalled: () => void;
}

interface NerEngineFieldsProps {
  engine: string;
  labels: readonly string[];
  labelsError?: string | null;
  onLabelsChange: (labels: string[]) => void;
  spacyDownload?: SpacyDownload | null;
}

/** Engine-specific NER authoring controls. Label state remains owned by the
 *  action form so switching engines cannot discard the user's schema. */
export function NerEngineFields({
  engine,
  labels,
  labelsError,
  onLabelsChange,
  spacyDownload,
}: NerEngineFieldsProps) {
  return (
    <>
      {engine === 'spacy' && spacyDownload && (
        <PinnedArtifactDownload
          artifact={spacyDownload.artifact}
          onInstalled={spacyDownload.onInstalled}
        />
      )}

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
        {engine === 'gliner' && (
          <>
            <div className="form-label">Labels</div>
            <p className="form-hint">
              GLiNER is zero-shot — type any label and press Enter or comma to add it.
            </p>
            <LabelChipsInput
              typed
              value={labels}
              onChange={onLabelsChange}
              placeholder="e.g. person, organization, ship name"
              ariaLabel="Entity labels"
              testId="ner-gliner-labels"
            />
            {labelsError && (
              <p className="form-error" data-testid="ner-gliner-labels-error" role="alert">
                {labelsError}
              </p>
            )}
          </>
        )}
        {engine === 'llm' && (
          <>
            <div className="form-label">Entity schema</div>
            <p className="form-hint">
              LLM extraction uses these labels as the structured entity types.
            </p>
            <LabelChipsInput
              typed
              value={labels}
              onChange={onLabelsChange}
              placeholder="e.g. person, organization, ship name"
              ariaLabel="LLM entity labels"
              testId="ner-llm-labels"
            />
            {labelsError && (
              <p className="form-error" data-testid="ner-llm-labels-error" role="alert">
                {labelsError}
              </p>
            )}
          </>
        )}
      </div>
    </>
  );
}
