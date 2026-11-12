import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createProject, importCsv, openAction, openProject, uniqueName } from './helpers';
import { exposeReplayModel, prepareOwnModelReplay, REPLAY_MODEL } from './replayModelScenario';

test.use({ deviceScaleFactor: 2 });

test('ordinary drawers recover refused settings without starting work', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-action-history'));
  await prepareOwnModelReplay(page.request, pid);
  await exposeReplayModel(page, pid);
  const sourceSheet = await importCsv(page.request, pid, 'sources.csv', 'story\nThe court dismissed the complaint.\n');
  const otherSheet = await importCsv(page.request, pid, 'other.csv', 'notes\nUnrelated material\n');
  const item = { id: 'submission:12', action_id: 'map.extract', sheet_id: sourceSheet,
    sheet_name: 'sources', submitted_at: '2026-11-12T10:12:00Z', status: 'not_started', run_id: null };
  const draft = { action_id: 'map.extract', scope: { kind: 'sheet_rows', sheet_id: sourceSheet, row_ids: null },
    params: { source: ['story'], engine: 'llm', model: REPLAY_MODEL,
      instruction: 'Identify the named party.', context: 'Retain this instruction after a credential failure.',
      fields: [{ name: 'party', type: 'text', description: 'The named party' }],
      include_confidence: false, include_justification: false }, output_names: { party: 'plaintiff' }, sheet_name: null };
  await page.route(`**/api/projects/${pid}/actions/history?*`, async route => {
    const action = new URL(route.request().url()).searchParams.get('action_id');
    await route.fulfill({ json: { items: action === 'map.extract' ? [item] : [], total: action === 'map.extract' ? 1 : 0, offset: 0, limit: 20 } });
  });
  await page.route(`**/api/projects/${pid}/actions/history/submission*`, route => route.fulfill({ json: { item, draft, unavailable_reason: null } }));
  const executions: string[] = [];
  page.on('request', request => {
    if (request.method() === 'POST' && /\/actions\/(?:v1\/run|preview-runs)(?:\?|$)/.test(new URL(request.url()).pathname)) {
      executions.push(request.url());
    }
  });
  await openProject(page, pid, otherSheet);
  await openAction(page, 'map.extract');
  await page.getByTestId('action-history-button').click();
  await expect(page.getByTestId('action-history-menu')).toContainText('Not started');
  await page.getByTestId('action-history-choose-submission:12').click();
  await expect(page).toHaveURL(new RegExp(`/s/${sourceSheet}/action/map.extract`));
  await expect(page.getByTestId('field-context')).toHaveValue(draft.params.context);
  await expect(page.getByTestId('field-output-party')).toHaveValue('plaintiff');
  await expect(page.getByTestId('text-source-columns')).toContainText('story');
  await expect(page.getByRole('button', { name: 'Replace columns', exact: true })).toHaveCount(0);
  expect(executions).toEqual([]);
  const out = process.env.FRISKET_HISTORY_SCREENSHOTS;
  if (out) {
    mkdirSync(out, { recursive: true });
    await page.screenshot({ path: path.join(out, 'history-restored-extract.png') });
  }
  await page.getByTestId('action-drawer-close').click();
  await openAction(page, 'map.clean_column');
  await page.getByTestId('action-history-button').click();
  await expect(page.getByTestId('action-history-menu')).toContainText('No earlier runs.');
  expect(executions).toEqual([]);
});
