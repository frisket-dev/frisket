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
  is_current: boolean;
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
    is_current: true,
  });
  return selected!;
}

async function confirmCostIfShown(page: Page): Promise<void> {
  const modal = page.getByTestId('cost-gate-modal');
  if (!await modal.isVisible({ timeout: 2_000 }).catch(() => false)) return;
  await page.getByTestId('cost-gate-input').fill('confirm');
  const confirm = page.getByTestId('cost-gate-confirm');
  await expect(confirm).toBeEnabled();
  await confirm.click();
}

async function clickAndConfirm(page: Page, buttonTestId: string): Promise<void> {
  await page.getByTestId(buttonTestId).click();
  await confirmCostIfShown(page);
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

  try {
    await page.goto(`/p/${projectId}/settings/project/ai-providers`);
    const routing = page.getByTestId('project-cloud-routing');
    const account = routing.getByLabel('Cloud account for this project');
    await expect(account).toBeVisible();
    await account.selectOption('41');
    await expect(account).toHaveValue('41');
    await routing.getByLabel('Use Investigations').check();
    await routing.getByLabel('Use your keys on this computer').check();
    await expect(routing.getByLabel('Use your keys on this computer')).toBeChecked();

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
    await page.keyboard.press('Escape');

    await waitForExtractSelector(page, () => page.reload());
    await expect(page.getByTestId('field-engine').locator('.engine-selector__trigger'))
      .toContainText('Gemini 3.5 Flash-Lite');

    await page.getByTestId('text-source-mode-template').click();
    await page.getByTestId('text-source-template-input').fill('{{text}}');
    await page.getByLabel('Field 1 name').fill('entity');
    await page.getByLabel('Field 1 type').selectOption('text');
    await page.getByLabel('Field 1 description').fill('Person named in the text');
    await page.getByTestId('action-prompt').fill('Extract the named person.');
    await page.getByTestId('field-citation_mode').selectOption('none');
    await expect(page.getByTestId('generated-action-preview')).toBeEnabled();

    await clickAndConfirm(page, 'generated-action-preview');
    await expect(page.getByTestId('preview-tab-stats')).toContainText(
      'Preview · 1 of 1 rows',
      { timeout: 30_000 },
    );
    await expect(page.getByTestId('preview-accounting')).toContainText('Model cost:');
    await expect(page.getByTestId('grid')).toContainText('Alice');
  } finally {
    await page.goto('about:blank');
    const deleted = await request.delete(`/api/projects/${projectId}`, {
      data: { confirm_name: name },
    });
    expect(deleted.ok()).toBeTruthy();
  }
});
