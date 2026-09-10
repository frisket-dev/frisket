import type { EngineOption } from '../../api/types';
import { engineTierLabel, engineUnavailableReason, tierForEngine } from '../../actions/engineCatalog';
import { ModelPicker, type ModelPickerChoice, type ModelPickerChoiceGroup } from '../ModelPicker';
import type { EngineModelChoicePresentation } from './GeneratedActionParamsBody';
import styles from './EngineModelChoice.module.css';

const ENGINE_CHOICE_PREFIX = 'engine:';

export interface EngineModelSelection {
  engine: string;
  model: string | null;
}

interface EngineModelChoiceProps {
  presentation: EngineModelChoicePresentation;
  engines: readonly EngineOption[];
  engine: string;
  model: string | null | undefined;
  onSelect(selection: EngineModelSelection): void;
}

/** One visible execution choice for actions that can select a fixed engine or
 * a provider-backed LLM model. Fixed leaves use ModelPicker's existing choice
 * groups; provider leaves stay backed by its live provider catalog. */
export function EngineModelChoice({
  presentation,
  engines,
  engine,
  model,
  onSelect,
}: EngineModelChoiceProps) {
  const providerEngine = engines.find((candidate) => candidate.id === presentation.providerEngineId);
  const fixed = engines.filter((candidate) => candidate.id !== presentation.providerEngineId);
  const knownFixed = fixed.some((candidate) => candidate.id === engine);
  const fixedChoices: ModelPickerChoice[] = fixed.map((candidate) => ({
    id: `${ENGINE_CHOICE_PREFIX}${candidate.id}`,
    label: candidate.label,
    note: [engineTierLabel(tierForEngine(candidate)), candidate.description]
      .filter(Boolean).join(' · '),
    available: candidate.available !== false,
    unavailableReason: engineUnavailableReason(candidate),
  }));
  if (engine && engine !== presentation.providerEngineId && !knownFixed) {
    fixedChoices.push({
      id: `${ENGINE_CHOICE_PREFIX}${engine}`,
      label: `${engine} (unavailable)`,
      available: false,
      unavailableReason: 'This engine is unavailable for this action.',
    });
  }
  const choiceGroups: ModelPickerChoiceGroup[] = [{
    id: 'fixed-engines',
    label: presentation.fixedGroupLabel ?? 'Engines',
    choices: fixedChoices,
  }];
  const selectedValue = engine === presentation.providerEngineId
    ? (model ?? '')
    : `${ENGINE_CHOICE_PREFIX}${engine}`;
  const labelId = `${presentation.engineParam}-model-choice-label`;

  return (
    <div className={styles.choice} data-testid={`field-${presentation.engineParam}-model-choice`}>
      <span className={`form-label ${styles.label}`} id={labelId}>{presentation.label}</span>
      <div className={styles.picker}>
        <ModelPicker
          value={selectedValue}
          choiceGroups={choiceGroups}
          providerModelsAvailable={providerEngine !== undefined && providerEngine.available !== false}
          providerModelsUnavailableReason={providerEngine
            ? engineUnavailableReason(providerEngine)
            : 'The LLM engine is unavailable for this action.'}
          onChange={(value) => {
            if (value.startsWith(ENGINE_CHOICE_PREFIX)) {
              onSelect({ engine: value.slice(ENGINE_CHOICE_PREFIX.length), model: null });
              return;
            }
            onSelect({ engine: presentation.providerEngineId, model: value });
          }}
          ariaLabelledBy={labelId}
        />
      </div>
    </div>
  );
}
