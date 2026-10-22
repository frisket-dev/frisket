import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createProject, importCsv } from '../e2e/helpers';
import { selectorChoicesResponse } from '../e2e/selectorChoicesFixture';

test('Ask offers response types through the generated form', async ({ page, context }) => {
  await context.route('**/*', route => new URL(route.request().url()).origin === process.env.FRISKET_INVENTORY_APP_URL
    ? route.continue() : route.abort());
  const pid = await createProject(page.request, 'Typed answers');
  const sid = await importCsv(page.request, pid, 'stories.csv', 'story\nThe council approved the new park.\n');
  await page.route(`**/api/projects/${pid}/selector-choices`, route => {
    const subject = route.request().postDataJSON().subject;
    const response = selectorChoicesResponse({
      projectId: pid, subject, currentChoiceId: 'example-model',
      groups: [{ id: 'hosted', label: 'Hosted', choices: [{
        choiceId: 'example-model', label: 'Claude Sonnet',
        authoredSelection: { kind: 'model', model: 'anthropic/example-model' },
        status: 'ready', canRun: true,
      }] }],
    });
    response.groups[0].choices[0].is_default = true;
    return route.fulfill({ json: response });
  });
  await page.goto(`/p/${pid}/s/${sid}/action/map.ask`);
  const form = page.getByTestId('generated-action-form');
  await expect(form).toBeVisible();
  await form.getByLabel('Question', { exact: true }).fill('Does this report mention a council decision?');
  const responseType = form.getByLabel('Response type', { exact: true });
  await expect(responseType).toHaveValue('text');
  await expect(responseType.locator('option')).toHaveCount(7);
  await responseType.selectOption('boolean');
  await expect(responseType).toHaveValue('boolean');
  const confidence = form.getByLabel('Include confidence', { exact: true });
  await expect(confidence).not.toBeChecked();
  await confidence.check();
  await expect(form.getByTestId('generated-action-run')).toBeEnabled();
  const dir = path.resolve('screenshots/ask-response-type');
  mkdirSync(dir, { recursive: true });
  await page.locator('.action-drawer').screenshot({ path: path.join(dir, 'boolean.png') });
  await responseType.selectOption('text');
  await expect(responseType).toHaveValue('text');
  await page.locator('.action-drawer').screenshot({ path: path.join(dir, 'text.png') });
});
