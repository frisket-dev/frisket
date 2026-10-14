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
  const pending = page.waitForRequest((request) => (
    request.method() === 'POST'
    && request.url().endsWith('/selector-choices')
  ));
  await navigate();
  const response = await (await pending).response();
  if (!response) throw new Error('Selector request did not complete');
  expect(response.ok()).toBeTruthy();
  const body = await response.json() as SelectorResponse;
  expect(body.subject).toMatchObject({
    kind: 'action',
    action_id: 'map.extract',
    field: 'engine',
  });
  const choices = body.groups.flatMap((group) => group.choices);
  const selected = choices.find((choice) => (
    choice.authored_selection.model === 'gemini/gemini-3.5-flash-lite'
  ));
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

async function fillExtractionForm(page: Page): Promise<void> {
  await page.getByTestId('text-source-mode-template').click();
  await page.getByTestId('text-source-template-input').fill('{{text}}');
  await page.getByLabel('Field 1 name').fill('entity');
  await page.getByLabel('Field 1 type').selectOption('text');
  await page.getByLabel('Field 1 description').fill('Person named in the text');
  await page.getByTestId('field-instruction').fill('Extract the named person.');
  await page.getByTestId('field-citation_mode').selectOption('none');
  await expect(page.getByTestId('generated-action-preview')).toBeEnabled();

}

async function confirmPreviewCost(page: Page): Promise<void> {
  const modal = page.getByTestId('cost-gate-modal');
  await expect(modal).toBeVisible();
  await expect(modal).toContainText('Investigations Cloud credits');
  const confirm = page.getByTestId('cost-gate-confirm');
  await expect(confirm).toBeEnabled();
  await confirm.click();
}

test('Per-model routes survive key setup and drive real Cloud and own previews', async ({
  page,
  request,
}, testInfo) => {
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

  await fillExtractionForm(page);

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
  // A working Cloud route must not hide the alternative credential form.
  await page.goto(actionURL);
  await page.getByTestId('field-engine').locator('.engine-selector__trigger').click();
  await dialog.getByRole('button', { name: 'Your Gemini key', exact: true }).click();
  await dialog.getByLabel('API key', { exact: true }).fill('e2e-personal-gemini-key');
  await dialog.getByRole('button', { name: 'Test', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Save', exact: true })).toBeEnabled();
  await dialog.screenshot({ path: testInfo.outputPath('cloud-own-key-setup.png') });
  const savedSelector = page.waitForResponse(async (response) => {
    if (response.request().method() !== 'POST' || !response.url().endsWith('/selector-choices') || !response.ok()) return false;
    const payload = await response.json() as SelectorResponse;
    return payload.groups.flatMap((group) => group.choices).some((choice) => (
      choice.authored_selection.model === 'gemini/gemini-3.5-flash-lite'
      && choice.execution_route?.kind === 'local_key' && choice.can_run
    ));
  });
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  const savedPayload = await (await savedSelector).json() as SelectorResponse;
  expect(savedPayload.groups.flatMap((group) => group.choices).find((choice) => (
    choice.authored_selection.model === 'gemini/gemini-3.5-flash'
  ))).toMatchObject({ execution_route: { kind: 'cloud_credits', account_id: 41 } });
  await expect(dialog.getByRole('button', { name: 'Your Gemini key', exact: true }))
    .toHaveAttribute('aria-pressed', 'true');
  await page.keyboard.press('Escape');

  // The saved own-key route links directly to the existing provider settings
  // at the narrow two-pane layout boundary.
  await page.setViewportSize({ width: 720, height: 900 });
  const narrowProjection = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && response.url().endsWith('/selector-choices')
    && response.ok()
  ));
  await page.goto(actionURL);
  const narrowPayload = await (await narrowProjection).json() as SelectorResponse;
  expect(narrowPayload.groups.flatMap((group) => group.choices).find((choice) => (
    choice.authored_selection.model === 'gemini/gemini-3.5-flash-lite'
  ))).toMatchObject({ execution_route: { kind: 'local_key' }, status: 'ready', can_run: true });
  await page.getByTestId('field-engine').locator('.engine-selector__trigger').click();
  await dialog.getByRole('searchbox').fill('Gemini 3.5 Flash-Lite');
  await dialog.locator(`[data-choice-id="${defaultChoice.choice_id}"]`).hover();
  await expect(dialog.getByRole('button', { name: 'Your Gemini key', exact: true }))
    .toHaveAttribute('title', 'Your Gemini key');
  await expect(dialog.getByRole('link', { name: 'Your Gemini API key →' })).toHaveAttribute(
    'href', `/p/${projectId}/settings/project/ai-providers`,
  );
  await expect(dialog.getByRole('button', { name: 'Change API key' })).toHaveCount(0);
  await dialog.screenshot({ path: testInfo.outputPath('narrow-configured-gemini-key.png') });
  await page.keyboard.press('Escape');
  await page.setViewportSize({ width: 1600, height: 900 });

  await fillExtractionForm(page);
  const ownPreview = page.waitForResponse(async (response) => (
    response.request().method() === 'GET'
    && response.url().includes(`/api/projects/${projectId}/actions/v1/preview/`)
    && response.ok() && (await response.json()).status === 'done'
  ));
  await page.getByTestId('generated-action-preview').click();
  const ownResult = await (await ownPreview).json();
  expect(Object.values(ownResult.result.rows)).toEqual([
    expect.objectContaining({ entity: expect.objectContaining({ value: 'Alice via own key' }) }),
  ]);

  await page.goto(actionURL);
  await page.getByTestId('field-engine').locator('.engine-selector__trigger').click();
  await dialog.getByRole('searchbox').fill('Gemini 3.5 Flash-Lite');
  await dialog.locator(`[data-choice-id="${defaultChoice.choice_id}"]`).hover();
  await waitForExtractSelector(page, () => dialog.getByRole('button', {
    name: 'Frisket Cloud', exact: true,
  }).click());
  await expect(dialog.getByRole('button', { name: 'Frisket Cloud', exact: true }))
    .toHaveAttribute('aria-pressed', 'true');
  await dialog.screenshot({ path: testInfo.outputPath('cloud-use-through-connected.png') });
  await page.keyboard.press('Escape');

  const visionOCRSelector = page.waitForResponse((response) => (
    response.request().method() === 'POST' && response.url().endsWith('/selector-choices') && response.ok()
  ));
  await page.goto(`/p/${projectId}/s/${sheetId}/action/media.ocr`);
  const visionOCRPayload = await (await visionOCRSelector).json() as SelectorResponse;
  const visionOCR = visionOCRPayload.groups.flatMap((group) => group.choices)
    .find((choice) => choice.authored_selection.engine === 'gemini/gemini-3.5-flash-lite');
  expect(visionOCR).toMatchObject({
    execution_route: { kind: 'cloud_credits', account_id: 41 },
    status: 'ready', can_run: true,
  });
  await page.getByTestId('field-engine').locator('.engine-selector__trigger').click();
  await dialog.getByRole('searchbox').fill('Gemini 3.5 Flash-Lite');
  await dialog.locator(`[data-choice-id="${visionOCR!.choice_id}"]`).hover();
  await expect(dialog.getByRole('button', { name: 'Frisket Cloud', exact: true }))
    .toHaveAttribute('aria-pressed', 'true');
  await dialog.getByRole('button', { name: 'Your Gemini key', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Your Gemini key', exact: true }))
    .toHaveAttribute('aria-pressed', 'true');
  await dialog.getByRole('button', { name: 'Frisket Cloud', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Frisket Cloud', exact: true }))
    .toHaveAttribute('aria-pressed', 'true');
  await expect(dialog).toContainText('$0.30 per million input tokens');
  await expect(dialog).toContainText('$2.50 per million output tokens');
  await expect(dialog).not.toContainText('per usd');
  await dialog.screenshot({ path: testInfo.outputPath('vision-ocr-cloud-route.png') });
  await page.keyboard.press('Escape');

  for (const [action, engineId] of [
    ['media.ocr', 'paddleocr-vl'],
    ['media.transcribe', 'whisper-turbo'],
  ]) {
    const mediaSelector = page.waitForResponse((response) => (
      response.request().method() === 'POST' && response.url().endsWith('/selector-choices') && response.ok()
    ));
    await page.goto(`/p/${projectId}/s/${sheetId}/action/${action}`);
    const payload = await (await mediaSelector).json() as SelectorResponse;
    const media = payload.groups.flatMap((group) => group.choices)
      .find((choice) => choice.authored_selection.engine === engineId);
    expect(media).toMatchObject({
      execution_route: { kind: 'cloud_credits', account_id: 41 },
      status: 'ready', can_run: true,
    });
    await page.getByTestId('field-engine').locator('.engine-selector__trigger').click();
    await dialog.getByRole('searchbox').fill(engineId === 'paddleocr-vl' ? 'Paddle' : 'Whisper');
    await dialog.locator(`[data-choice-id="${media!.choice_id}"]`).click();
    await page.getByTestId('field-engine').locator('.engine-selector__trigger').click();
    await expect(dialog).toContainText('Frisket Cloud');
    await dialog.getByRole('button', { name: 'Models server', exact: true }).click();
    const guide = dialog.getByRole('link', { name: 'Models server setup guide' });
    const url = dialog.getByLabel('Gateway URL');
    const token = dialog.getByLabel('Gateway token');
    const testButton = dialog.getByRole('button', { name: 'Test', exact: true });
    const boxes = await Promise.all([guide, url, token, testButton].map((item) => item.boundingBox()));
    for (let index = 1; index < boxes.length; index++) {
      expect(boxes[index]!.y).toBeGreaterThanOrEqual(boxes[index - 1]!.y + boxes[index - 1]!.height);
    }
    await dialog.screenshot({ path: testInfo.outputPath(`${engineId}-cloud-gateway.png`) });
    await testButton.scrollIntoViewIfNeeded();
    await dialog.screenshot({ path: testInfo.outputPath(`${engineId}-gateway-fields.png`) });
    await page.keyboard.press('Escape');
  }

  await page.setViewportSize({ width: 720, height: 900 });
  const openDocRouterSelector = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && response.url().endsWith('/selector-choices')
    && response.ok()
  ));
  await page.goto(`/p/${projectId}/s/${sheetId}/action/media.ocr`);
  const openDocRouterPayload = await (await openDocRouterSelector).json() as SelectorResponse;
  const openDocRouter = openDocRouterPayload.groups.flatMap((group) => group.choices)
    .find((choice) => choice.authored_selection.engine?.startsWith('opendocrouter/'));
  expect(openDocRouter).toBeDefined();
  await page.getByTestId('field-engine').locator('.engine-selector__trigger').click();
  await dialog.getByRole('searchbox').fill('OpenDocRouter');
  await dialog.locator(`[data-choice-id="${openDocRouter!.choice_id}"]`).hover();
  const openDocRouterRoute = dialog.getByRole('button', {
    name: 'Your OpenDocRouter key', exact: true,
  });
  await expect(openDocRouterRoute).toHaveAttribute('title', 'Your OpenDocRouter key');
  const openDocRouterWidths = await dialog.evaluate((element) => {
    const routeControl = element.querySelector<HTMLElement>('.engine-selector__route-control-body')!;
    const button = element.querySelector<HTMLElement>('[data-testid="model-route-own"]')!;
    const controlBounds = routeControl.getBoundingClientRect();
    const buttonBounds = button.getBoundingClientRect();
    return {
      dialog: [element.scrollWidth, element.clientWidth],
      buttonWithinControl: buttonBounds.right <= controlBounds.right + 1,
    };
  });
  expect(openDocRouterWidths.dialog[0]).toBeLessThanOrEqual(openDocRouterWidths.dialog[1]);
  expect(openDocRouterWidths.buttonWithinControl).toBe(true);
  await dialog.screenshot({ path: testInfo.outputPath('narrow-opendocrouter-route.png') });
  await page.keyboard.press('Escape');
  await page.setViewportSize({ width: 1600, height: 900 });

  await page.goto(`/p/${projectId}/s/${sheetId}/action/media.ocr`);
  await page.getByTestId('field-engine').locator('.engine-selector__trigger').click();
  await dialog.getByRole('searchbox').fill('Datalab');
  // A blocked model can still be inspected and set up in the detail pane.
  await dialog.locator('[data-choice-id]').filter({ hasText: 'Datalab' }).first().hover();
  await expect(dialog.getByRole('link', { name: 'Learn more', exact: true }))
    .toHaveAttribute('href', 'https://cloud.example.test/funding-accounts?account_id=41&provider=datalab');
  await dialog.screenshot({ path: testInfo.outputPath('cloud-account-key-setup.png') });

  await page.goto('/settings/personal/profile');
  await page.getByTestId('cloud-connection-connected')
    .getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Sign in with browser' })).toBeVisible();
  await page.goto(actionURL);
  await page.getByTestId('field-engine').locator('.engine-selector__trigger').click();
  await dialog.getByRole('searchbox').fill('Gemini 3.5 Flash-Lite');
  await dialog.locator(`[data-choice-id="${defaultChoice.choice_id}"]`).hover();
  await expect(dialog.getByRole('button', { name: 'Frisket Cloud', exact: true }))
    .toHaveAttribute('aria-pressed', 'true');
  const reconnect = dialog.getByRole('button', { name: 'Reconnect', exact: true });
  await expect(reconnect).toBeEnabled();
  await dialog.screenshot({ path: testInfo.outputPath('cloud-use-through-disconnected.png') });
  await waitForExtractSelector(page, () => reconnect.click());
  await expect(dialog).toContainText('$4.50 balance');
  await dialog.getByRole('button', { name: 'Your Gemini key', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Your Gemini key', exact: true }))
    .toHaveAttribute('aria-pressed', 'true');
});
