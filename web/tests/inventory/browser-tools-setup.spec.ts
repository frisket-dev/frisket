import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createProject, importCsv } from '../e2e/helpers';

/** Route fixtures exercise the real drawer, setup transport and status notice. */
test('browser tools setup preserves the action draft and requires an explicit run', async ({ page, context }) => {
  await context.route('**/*', route => new URL(route.request().url()).origin === process.env.FRISKET_INVENTORY_APP_URL
    ? route.continue() : route.abort());
  const pid = await createProject(page.request, 'Browser tools setup');
  const sid = await importCsv(page.request, pid, 'urls.csv', 'url\nhttps://example.com\n');
  const output = path.resolve('screenshots/browser-tools-setup');
  mkdirSync(output, { recursive: true });
  let ready = false;
  let starts = 0;
  let runs = 0;
  const pull = { schemaVersion: 'frisket.model_pull.v4', id: 7001, operation_kind: 'engine_setup',
    display_name: 'Browser tools (Chromium)', model: 'engine-setup:chromium.local@1', status: 'running',
    capabilities: { cancel: true, retry: false, remove: false }, endpoint_id: null, endpoint_origin: null,
    cancel_requested: false, error: null, created_at: '2026-10-29T00:00:00Z', started_at: null,
    finished_at: null, initiated_by: null, phase: 'provisioning', completed_bytes: 0,
    total_bytes: null, resolved_digest: null, resolved_size: null, artifact: null };
  await page.route(`**/api/projects/${pid}/actions/v1/validate-params`, async route => {
    const action = route.request().postDataJSON().action;
    return route.fulfill({ json: { schema_version: 'frisket.action_param_validation_result.v1',
      project_id: String(pid), action: { action_id: action.action_id }, diagnostics: {},
      logical_outputs: [{ key: 'screenshot', column_type: 'file', existing_column_policy: 'compatible' }],
      runtime_setup: ready ? null : { kind: 'engine_setup', scope: 'workspace', setup_ref: pull.model,
        can_start: true, can_mutate: true, blocked_by_operation: null },
    } });
  });
  await page.route('**/api/providers/models/setup', route => {
    starts += 1;
    return route.fulfill({ json: { pull, deduplicated: false } });
  });
  await page.route('**/api/providers/models/pulls/7001', route => route.fulfill({ json: {
    ...pull, ...(ready ? { status: 'done', phase: 'done' } : {}),
  } }));
  await page.route(`**/api/projects/${pid}/actions/v1/run`, route => { runs += 1; return route.abort(); });
  await page.goto(`/p/${pid}/s/${sid}/action/web.capture_screenshot`);
  const form = page.getByTestId('generated-action-form');
  const run = page.getByTestId('generated-action-run');
  await expect(form.getByRole('button', { name: 'Set up browser tools' })).toBeVisible();
  await expect(run).toBeDisabled();
  await form.getByTestId('field-viewport_width').fill('1440');
  await form.getByTestId('field-output-screenshot').fill('Saved screenshot');
  await form.getByTestId('field-viewport_width').click();
  await page.screenshot({ path: path.join(output, '01-setup-required.png') });
  expect(starts).toBe(0);
  await form.getByRole('button', { name: 'Set up browser tools' }).click();
  const activity = page.getByTestId('status-notice-stack').getByTestId('model-pull-progress');
  await expect(activity).toBeVisible();
  await expect(activity.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow');
  await expect(activity.locator('.activity-progress-bar.indeterminate')).toBeVisible();
  await expect(activity.locator('.model-pull-progress-size')).toHaveCount(0);
  await page.screenshot({ path: path.join(output, '02-downloading.png') });
  ready = true;
  await expect(run).toBeEnabled();
  await expect(form.getByTestId('field-viewport_width')).toHaveValue('1440');
  await expect(form.getByTestId('field-output-screenshot')).toHaveValue('Saved screenshot');
  await page.screenshot({ path: path.join(output, '03-ready-no-autorun.png') });
  expect(starts).toBe(1);
  expect(runs).toBe(0);
});
