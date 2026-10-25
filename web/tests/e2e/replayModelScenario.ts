import type { APIRequestContext, Page } from '@playwright/test';

import { setProjectModelRoute } from './helpers';
import {
  actionSelectorResponse,
  stubActionSelectorChoices,
  type SelectorGroupFixture,
} from './selectorChoicesFixture';

export const REPLAY_MODEL = 'gemini/gemini-3.5-flash-lite';

/**
 * Keep replay-backed browser scenarios on the model and route used to prime
 * their cache. A model-only setup can silently fall back to Cloud routing and
 * miss the local replay fixture.
 */
export async function prepareOwnModelReplay(
  request: APIRequestContext,
  projectId: string,
): Promise<void> {
  await setProjectModelRoute(request, projectId, REPLAY_MODEL, 'own');
}

/** Expose the strict-replay model as runnable through the typed selector
 * facade. Action execution still uses the live backend and replay cache. */
export async function exposeReplayModel(page: Page, projectId: string): Promise<void> {
  const engineGroups: SelectorGroupFixture[] = [{
    id: 'gemini', label: 'Gemini', choices: [{
      choiceId: 'replay-engine-model', label: 'Gemini 3.5 Flash-Lite',
      summary: 'Replay-backed hosted model',
      authoredSelection: { kind: 'engine_model', engine: 'llm', model: REPLAY_MODEL },
    }],
  }];
  const modelGroups: SelectorGroupFixture[] = [{
    id: 'gemini', label: 'Gemini', choices: [{
      choiceId: 'replay-model', label: 'Gemini 3.5 Flash-Lite',
      summary: 'Replay-backed hosted model',
      authoredSelection: { kind: 'model', model: REPLAY_MODEL },
    }],
  }];
  await stubActionSelectorChoices(page, projectId, ({ actionId, field, params }) => {
    const engineField = field === 'engine';
    const groups = engineField ? engineGroups : modelGroups;
    const selected = engineField
      ? params.engine === 'llm' && params.model === REPLAY_MODEL
      : params.model === REPLAY_MODEL;
    return actionSelectorResponse({
      projectId, actionId, field, groups,
      currentChoiceId: selected ? groups[0].choices[0].choiceId : null,
    });
  });
}
