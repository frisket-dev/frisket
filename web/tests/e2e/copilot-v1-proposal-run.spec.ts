import { expect, test } from '@playwright/test';
import { createProject, importCsv, uniqueName } from './helpers';

const CSV =
  'story\n' +
  '"The city council approved a $4M paving contract on Tuesday."\n';

// Local browser smoke only. The fetch-transport unit contract owns the
// exhaustive strict-Copilot-wire ∩ migrated-kind matrix, so this e2e stays
// focused on the visible Copilot-to-run interaction instead of duplicating it.
test('copilot classifier proposal direct Run posts a typed action request', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-copilot-v1-run'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', CSV);
  const legacyRunPosts: unknown[] = [];
  const v1Posts: Record<string, unknown>[] = [];
  const runId = 72001;

  await page.route(`**/api/projects/${pid}/copilot`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reply: 'I can add a classifier action.',
        needs_import: false,
        proposals: [
          {
            kind: 'map',
            title: 'Classify by News Beat',
            spec: {
              action_id: 'map.classify',
              scope: { kind: 'sheet_rows', sheet_id: sheetId },
              output_names: { news_beat: 'news_beat', news_beat_confidence: 'news_beat_confidence', news_beat_justification: 'news_beat_justification' },
              params: {
                engine: 'llm',
                source: ['story'],
                context: 'Classify each story by local news beat.',
                model: 'gemini/gemini-2.5-flash',
                fields: [
                  {
                    name: 'news_beat',
                    type: 'category',
                    description: 'Best-fit local news beat',
                    labels: ['government', 'business', 'other'],
                  },
                ],
                include_confidence: true,
                include_justification: true,
              },
            },
          },
        ],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/run`, async (route) => {
    legacyRunPosts.push(route.request().postDataJSON());
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'copilot v1 proposal should not use legacy run' }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    v1Posts.push(body);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: body.action_id, action_id: 'act-web-copilot-classify' },
        status: 'completed',
        run_id: runId,
        receipt_id: 'receipt-web-copilot-classify',
        outputs: [],
        errors: [],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/runs/${runId}/status`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.actions.v1',
        action: 'run_status',
        project_id: pid,
        run: {
          id: runId,
          sheet_id: Number(sheetId),
          action_kind: 'map.classify',
          action_name: 'Classify by News Beat',
          status: 'completed',
          total_rows: 1,
          completed_rows: 1,
          failed_rows: 0,
          cost_actual: 0,
          cost_estimate: 0,
          public_status: {
            run_id: runId,
            action_kind: 'map.classify',
            action_name: 'Classify by News Beat',
            status: 'completed',
            live: false,
            completed: 1,
            total: 1,
            failed: 0,
            cost: 0,
            timing: {
              started_at: null,
              finished_at: null,
              elapsed_seconds: null,
              processed_rows: 1,
              remaining_rows: 0,
              processed_rows_per_second: null,
              eta_seconds: null,
              estimated_finish_at: null,
              queue_wait_seconds: null,
              job_elapsed_seconds: null,
            },
          },
        },
      }),
    });
  });

  await page.goto(`/p/${pid}`);
  await page.getByTestId('chrome-copilot-toggle').click();
  await page.getByTestId('copilot-input').fill('Classify these stories');
  await page.getByTestId('copilot-send').click();
  await expect(page.getByTestId('copilot-proposal')).toContainText('Classify by News Beat');

  await page.getByTestId('copilot-run').click();
  await expect.poll(() => v1Posts.length).toBe(1);
  expect(legacyRunPosts).toHaveLength(0);

  const posted = v1Posts[0];
  expect(posted.action_id).toBe('map.classify');
  expect(posted.scope).toEqual({ kind: 'sheet_rows', sheet_id: sheetId });
  expect(posted.idempotency_key).toMatch(/^web-map\.classify:/);

  const params = posted.params as Record<string, unknown>;
  expect(params.source).toEqual(['story']);
  expect(params).not.toHaveProperty('sheet_id');
  expect(params.context).toBe('Classify each story by local news beat.');
  expect(params.model).toBe('gemini/gemini-2.5-flash');
  expect(params).not.toHaveProperty('debug');
  expect(params).not.toHaveProperty('confirmed');
  expect(params.fields).toEqual([
    expect.objectContaining({
      name: 'news_beat',
      type: 'category',
      description: 'Best-fit local news beat',
      labels: ['government', 'business', 'other'],
    }),
  ]);
  await expect(page.getByTestId('run-progress')).toContainText('complete');
});
