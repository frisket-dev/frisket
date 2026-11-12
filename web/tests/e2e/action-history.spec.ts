import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createProject, importCsv, openAction, openProject, uniqueName } from './helpers';
import { exposeReplayModel, REPLAY_MODEL } from './replayModelScenario';

test.use({ deviceScaleFactor: 2 });

test('ordinary drawers recover refused settings without starting work', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-action-history'));
  await exposeReplayModel(page, pid);
  const sourceSheet = await importCsv(page.request, pid, 'sources.csv', 'story\nThe court dismissed the complaint.\n');
  const otherSheet = await importCsv(page.request, pid, 'other.csv', 'notes\nUnrelated material\n');
  const draft = { action_id: 'map.extract', scope: { kind: 'sheet_rows', sheet_id: sourceSheet },
    params: { source: ['story'], engine: 'llm', model: REPLAY_MODEL,
      instruction: 'Identify the named party.', context: 'Retain this instruction after a credential failure.',
      fields: [{ name: 'party', type: 'text', description: 'The named party' }],
      include_confidence: false }, output_names: { party: 'plaintiff' } };
  const refused = await page.request.post(`/api/projects/${pid}/actions/v1/run`, {
    data: { ...draft, idempotency_key: `history-refusal:${pid}` },
  });
  const result = await refused.json();
  expect(result.status).toBe('failed');
  expect(result.errors).toEqual(expect.arrayContaining([expect.objectContaining({ code: 'missing_provider_key' })]));
  expect(result.run_id ?? null).toBeNull();
  const historyResponse = await page.request.get(`/api/projects/${pid}/actions/history`, {
    params: { action_id: draft.action_id },
  });
  expect(historyResponse.ok()).toBeTruthy();
  const history = await historyResponse.json();
  expect(history.items).toHaveLength(1);
  const item = history.items[0];
  expect(item).toMatchObject({ action_id: draft.action_id, sheet_id: sourceSheet,
    status: 'not_started', run_id: null });
  const out = process.env.FRISKET_HISTORY_SCREENSHOTS;
  if (out) mkdirSync(out, { recursive: true });
  const executions: string[] = [];
  page.on('request', request => {
    if (request.method() === 'POST' && /\/actions\/(?:v1\/run|preview-runs)(?:\?|$)/.test(new URL(request.url()).pathname)) {
      executions.push(request.url());
    }
  });
  await openProject(page, pid, otherSheet);
  await page.reload();
  await openAction(page, 'map.extract');
  await page.getByTestId('action-history-button').click();
  await expect(page.getByTestId('action-history-menu')).toContainText('Not started');
  if (out) await page.screenshot({ path: path.join(out, 'history-refused-menu.png') });
  await page.getByTestId(`action-history-choose-${item.id}`).click();
  await expect(page).toHaveURL(new RegExp(`/s/${sourceSheet}/action/map.extract`));
  await expect(page.getByTestId('field-context')).toHaveValue(draft.params.context);
  await expect(page.getByTestId('field-output-party')).toHaveValue('plaintiff');
  await expect(page.getByTestId('text-source-columns')).toContainText('story');
  await expect(page.getByRole('button', { name: 'Replace columns', exact: true })).toHaveCount(0);
  expect(executions).toEqual([]);
  if (out) await page.screenshot({ path: path.join(out, 'history-restored-extract.png') });
  await page.getByTestId('action-drawer-close').click();
  await openAction(page, 'map.clean_column');
  await page.getByTestId('action-history-button').click();
  await expect(page.getByTestId('action-history-menu')).toContainText('No earlier runs.');
  expect(executions).toEqual([]);
});
