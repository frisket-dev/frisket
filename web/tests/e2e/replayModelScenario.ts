import type { APIRequestContext } from '@playwright/test';

import { setProjectModelRoute } from './helpers';

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
