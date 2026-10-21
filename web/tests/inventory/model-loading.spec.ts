import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createProject, importCsv } from '../e2e/helpers';
import { selectorChoicesResponse } from '../e2e/selectorChoicesFixture';

test('model guidance follows real selector loading, setup and recovery states', async ({ page, context }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', route => new URL(route.request().url()).origin === process.env.FRISKET_INVENTORY_APP_URL
    ? route.continue() : route.abort());
  const pid = await createProject(page.request, 'Model availability');
  const sid = await importCsv(page.request, pid, 'stories.csv', 'story\nThe council approved the new park.\n');
  const output = path.resolve('screenshots/model-loading');
  mkdirSync(output, { recursive: true });
  type State = 'loading' | 'choose' | 'empty' | 'setup' | 'working' | 'unavailable' | 'error' | 'ready';
  let state: State = 'loading';
  let release: (() => void) | undefined;
  await page.route(`**/api/projects/${pid}/selector-choices`, async route => {
    const subject = route.request().postDataJSON().subject;
    if (subject.kind !== 'action') return route.fallback();
    if (state === 'loading') await new Promise<void>(resolve => { release = resolve; });
    if (state === 'error') return route.fulfill({ status: 503, json: { detail: 'Model list temporarily unavailable.' } });
    const response = selectorChoicesResponse({
      projectId: pid, subject,
      currentChoiceId: state === 'choose' || state === 'empty' ? null : 'example-model',
      groups: state === 'empty' ? [] : [{ id: 'hosted', label: 'Hosted', choices: [{
        choiceId: 'example-model', label: 'Gemini Flash',
        authoredSelection: { kind: 'model', model: 'gemini/example-model' },
        status: state === 'setup' ? 'needs_setup' : state === 'working' ? 'working'
          : state === 'unavailable' ? 'unavailable' : 'ready',
        canRun: state === 'ready' || state === 'choose',
        blocker: state === 'setup' ? 'Add an API key to use this provider.'
          : state === 'unavailable' ? 'This model is unavailable on this server.' : null,
      }] }],
    });
    const selected = response.groups[0]?.choices[0];
    if (selected) {
      selected.is_default = state !== 'choose';
      if (state === 'setup') Object.assign(selected, {
        blocker: { code: 'provider_key_required', field: 'model', message: 'Add an API key to use this provider.' },
        setup: { kind: 'api_key', provider: 'gemini', scopes: [{
          scope: 'project', configured: false, can_mutate: true, source: 'missing',
          environment_names: ['GEMINI_API_KEY'], settings_location: 'project_ai_providers', hint: null,
        }] },
      });
    }
    return route.fulfill({ json: response });
  });
  const form = page.getByTestId('generated-action-form');
  const run = page.getByTestId('generated-action-run');
  const capture = async (name: string, message: string | null) => {
    await expect(form).toBeVisible();
    await expect(run).toBeVisible();
    if (message) {
      await expect(form.getByText(message, { exact: true }).first()).toBeVisible();
      await expect(run).toBeDisabled();
    } else {
      await expect(form.getByText('Setup needed', { exact: true })).toHaveCount(0);
      await expect(form.getByText('Loading models…', { exact: true })).toHaveCount(0);
    }
    await page.evaluate(() => document.fonts.ready);
    await page.locator('.action-drawer').screenshot({ path: path.join(output, `${name}.png`) });
  };
  const url = `/p/${pid}/s/${sid}/action/map.summarize`;
  await page.goto(url);
  await capture('01-loading', 'Loading models…');
  state = 'choose';
  release?.();
  await capture('02-choose', 'Choose a model');
  for (const [next, message] of [
    ['empty', 'No models available'], ['setup', 'Setup needed'],
    ['working', 'Model setup in progress…'],
    ['unavailable', 'This model is unavailable on this server.'],
    ['error', 'Couldn’t load models'],
  ] as const) {
    state = next;
    await page.goto(url);
    await capture(`${next}`, message);
  }
  state = 'ready';
  await form.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(form.getByTestId('engine-selector')).toBeVisible();
  await expect(form.getByText('Couldn’t load models', { exact: true })).toHaveCount(0);
  await capture('08-ready', null);
  await expect(run).toBeEnabled();
  await page.screenshot({ path: path.join(output, '09-ready-context.png') });
  expect(errors).toEqual([]);
});
