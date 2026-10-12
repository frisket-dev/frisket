import { expect, test, type Page } from '@playwright/test';

import {
  createProject,
  importCsv,
  uniqueName,
} from './helpers';

type SelectorChoice = {
  choice_id: string;
  authored_selection: { kind: string; engine?: string | null; model?: string | null };
  execution_route: {
    kind: string;
    account_id: number | null;
    balance_label: string | null;
  } | null;
  status: string;
  can_run: boolean;
};

type SelectorResponse = {
  subject: { kind: string; action_id?: string; field?: string };
  default_choice_id: string | null;
  groups: Array<{ choices: SelectorChoice[] }>;
};

async function waitForExtractSelector(page: Page, navigate: () => Promise<unknown>) {
  const pending = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && response.url().endsWith('/selector-choices')
  ));
  await navigate();
  const response = await pending;
  expect(response.ok()).toBeTruthy();
  const body = await response.json() as SelectorResponse;
  expect(body.subject).toMatchObject({
    kind: 'action',
    action_id: 'map.extract',
    field: 'engine',
  });
  const choices = body.groups.flatMap((group) => group.choices);
  const selected = choices.find((choice) => choice.choice_id === body.default_choice_id);
  expect(selected).toMatchObject({
    authored_selection: {
      kind: 'engine_model',
      engine: 'llm',
      model: 'gemini/gemini-3.5-flash-lite',
    },
    execution_route: {
      kind: 'cloud_credits',
      account_id: 41,
      balance_label: '$4.50 balance',
    },
    status: 'ready',
    can_run: true,
  });
  return selected!;
}

async function confirmPreviewCost(page: Page): Promise<void> {
  const modal = page.getByTestId('cost-gate-modal');
  await expect(modal).toBeVisible();
  await expect(modal).toContainText('Investigations Cloud credits');
  const confirm = page.getByTestId('cost-gate-confirm');
  await expect(confirm).toBeEnabled();
  await confirm.click();
}

test('browser sign-in makes Cloud credits the durable Extract default and previews without a local key', async ({
  page,
  request,
}) => {
  await page.addInitScript(() => {
    localStorage.setItem(
      'frisket:product-telemetry:v1',
      JSON.stringify({ schema_version: 1, state: 'disabled' }),
    );
  });

  await page.goto('/settings/personal/profile');
  const cloudProfile = page.getByTestId('cloud-profile-settings');
  await expect(cloudProfile.getByTestId('cloud-connection-card')).toBeVisible();
  await cloudProfile.getByRole('button', { name: 'Sign in with browser' }).click();

  const waiting = cloudProfile.getByTestId('cloud-connection-waiting');
  await expect(waiting).toBeVisible();
  await expect(waiting).toContainText('CLOUD-E2E');
  await expect(waiting.getByRole('link', { name: 'Open sign-in page' })).toHaveAttribute(
    'href',
    /cloud\.example\.test\/device.*CLOUD-E2E/,
  );
  const connected = cloudProfile.getByTestId('cloud-connection-connected');
  await expect(connected).toContainText('Cloud E2E Reporter', { timeout: 20_000 });
  await expect(connected).toContainText('Investigations');
  await expect(connected).toContainText('$4.50 balance');

  const name = uniqueName('e2e-cloud-extract');
  const projectId = await createProject(request, name);
  const sheetId = await importCsv(
    request,
    projectId,
    'people.csv',
    'text\n"Alice signed the agreement."\n',
  );

  await page.goto(`/p/${projectId}/settings/project/ai-providers`);
  const routing = page.getByTestId('project-cloud-routing');
  const account = routing.getByLabel('Cloud account for this project');
  await expect(account).toBeVisible();
  await account.selectOption('41');
  await expect(account).toHaveValue('41');
  await expect(routing.getByLabel('Use your keys on this computer')).toBeChecked();
  await expect(account).toBeEnabled();

  const actionURL = `/p/${projectId}/s/${sheetId}/action/map.extract`;
  const defaultChoice = await waitForExtractSelector(page, () => page.goto(actionURL));
  const engine = page.getByTestId('field-engine');
  const trigger = engine.locator('.engine-selector__trigger');
  await expect(trigger).toContainText('Gemini 3.5 Flash-Lite');
  await trigger.click();
  const dialog = page.getByTestId('engine-selector-dialog');
  const selected = dialog.locator(`[data-choice-id="${defaultChoice.choice_id}"]`);
  await expect(selected).toHaveAttribute('aria-current', 'true');
  await expect(dialog).toContainText('Frisket Cloud');
  await expect(dialog).toContainText('$4.50 balance');
  await expect(dialog.getByPlaceholder('API key', { exact: true })).toHaveCount(0);
  await page.keyboard.press('Escape');

  await waitForExtractSelector(page, () => page.reload());
  await expect(page.getByTestId('field-engine').locator('.engine-selector__trigger'))
    .toContainText('Gemini 3.5 Flash-Lite');

  await page.getByTestId('text-source-mode-template').click();
  await page.getByTestId('text-source-template-input').fill('{{text}}');
  await page.getByLabel('Field 1 name').fill('entity');
  await page.getByLabel('Field 1 type').selectOption('text');
  await page.getByLabel('Field 1 description').fill('Person named in the text');
  await page.getByTestId('field-instruction').fill('Extract the named person.');
  await page.getByTestId('field-citation_mode').selectOption('none');
  await expect(page.getByTestId('generated-action-preview')).toBeEnabled();

  const completed = page.waitForResponse(async (response) => (
    response.request().method() === 'GET'
    && response.url().includes(`/api/projects/${projectId}/actions/v1/preview/`)
    && response.ok()
    && (await response.json()).status === 'done'
  ));
  await page.getByTestId('generated-action-preview').click();
  await confirmPreviewCost(page);
  const result = await (await completed).json();
  // Preview reports provider model cost; the fixture's Cloud charge is separate.
  expect(result.accounting).toMatchObject({
    status: 'completed', model_call_count: 1, cost_actual: 0.000006,
  });
  expect(Object.values(result.result.rows)).toEqual([
    expect.objectContaining({ entity: expect.objectContaining({ value: 'Alice' }) }),
  ]);
  await expect(page.getByTestId('preview-tab-stats')).toContainText(
    'Preview · 1 of 1 rows',
    { timeout: 30_000 },
  );
  await expect(page.getByTestId('preview-accounting')).toContainText('Model cost:');
});
