import { useId, useRef, useState } from 'react';
import type { SelectorChoice } from '../../api/selectorChoices';
import { SelectorField } from '../../engine-selector/SelectorField';
import { CloudSetupPanel } from '../../engine-selector/SelectorSetup';
import { notifyCloudConnectionChanged } from '../../cloud/connectionChanged';

/** One selector owns both the saved model and the guidance for making it usable. */
export function AskModelSetup({ projectId, model, onSelect, onCurrentChoiceChange }: {
  projectId: string;
  model: string | null;
  onSelect(choice: SelectorChoice): void;
  onCurrentChoiceChange(choice: SelectorChoice | null): void;
}) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [cloudOpen, setCloudOpen] = useState(false);
  const cloudPanelId = useId();
  return <div className="ask-model-setup">
    <SelectorField projectId={projectId} label="Model" recentNamespace={`${projectId}:ask`}
      triggerRef={trigger}
      query={{ schema_version: 'frisket.selector_choices_query.v1',
        subject: { kind: 'project_ask', ...(model ? { model } : {}) } }}
      onCurrentChoiceChange={onCurrentChoiceChange} onSelect={onSelect}
      renderSetupPrompt={({ currentChoice, cloudSetup, hasChoices, hasRunnableChoices }) => {
        if (currentChoice?.can_run) return null;
        const setup = currentChoice?.setup?.kind === 'cloud' ? currentChoice.setup : cloudSetup;
        const canConnect = !hasRunnableChoices && setup
          && ['connect', 'reconnect', 'account_required'].includes(setup.state);
        if (!hasChoices && !canConnect) return null;
        return <div className="ask-model-setup-prompt">
          <p className="form-hint">{hasRunnableChoices
            ? 'Choose an available model to start chatting.'
            : canConnect ? hasChoices ? 'Sign in to Frisket Cloud or set up a model to start chatting.'
              : 'Sign in to Frisket Cloud to see available chat models.'
              : 'Set up a model to start chatting.'}</p>
          <div className="ask-model-setup-actions">
            {canConnect && <button type="button" className="btn btn-primary"
              aria-expanded={cloudOpen} aria-controls={cloudPanelId}
              onClick={() => setCloudOpen((open) => !open)}>Sign in to Frisket Cloud</button>}
            {hasChoices && <button type="button" className="btn" onClick={() => trigger.current?.click()}>
              {hasRunnableChoices ? 'Choose a model' : 'Set up a model'}
            </button>}
          </div>
          {canConnect && cloudOpen && <div id={cloudPanelId}>
            <CloudSetupPanel key={`${projectId}:${setup.state}`} setup={setup}
              projectId={projectId} onChanged={notifyCloudConnectionChanged} />
          </div>}
        </div>;
      }} />
  </div>;
}
