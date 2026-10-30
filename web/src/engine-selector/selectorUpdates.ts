import type { SelectorChoice } from '../api/selectorChoices';

/** Translate the selector's authored choice into the action's semantic fields. */
export function selectorUpdates(field: string, modelField: string | undefined, choice: SelectorChoice) {
  const authored = choice.authored_selection;
  return authored.kind === 'engine' ? { [field]: authored.engine }
    : authored.kind === 'model' ? { [field]: authored.model }
      : authored.kind === 'engine_model' && modelField ? { [field]: authored.engine, [modelField]: authored.model }
        : null;
}
