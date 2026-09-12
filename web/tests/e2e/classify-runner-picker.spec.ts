import { expect, test } from '@playwright/test';
import { createProject, importCsv, openAction, uniqueName } from './helpers';
import {
  actionSelectorResponse,
  stubActionSelectorChoices,
  type SelectorGroupFixture,
} from './selectorChoicesFixture';

test('an open native selector retries a failed draft refresh in its dialog', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-selector-retry'));
  await importCsv(page.request, pid, 'stories.csv', 'headline,alternate\n"Transit service expanded","Transit service paused"\n');
  let failRefresh = false;
  let requests = 0;
  await page.route(`**/api/projects/${pid}/selector-choices`, async (route) => {
    const body = route.request().postDataJSON() as {
      subject?: { kind?: string; action_id?: string; field?: string };
    };
    if (body.subject?.kind !== 'action'
      || body.subject.action_id !== 'map.classify'
      || body.subject.field !== 'engine') {
      await route.fallback();
      return;
    }
    requests += 1;
    if (failRefresh) {
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'temporary selector outage' }),
      });
      return;
    }
    const response = actionSelectorResponse({
      projectId: pid,
      actionId: 'map.classify',
      field: 'engine',
      currentChoiceId: 'local-semantic',
      groups: [{
        id: 'local',
        label: 'Local',
        choices: [{
          choiceId: 'local-semantic',
          label: 'Local semantic',
          summary: 'Runs on this computer',
          authoredSelection: { kind: 'engine', engine: 'local_semantic' },
        }],
      }],
    });
    response.depends_on = ['engine', 'model', 'context'];
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(response),
    });
  });

  await page.goto(`/p/${pid}`);
  await openAction(page, 'map.classify');
  const trigger = page.getByTestId('field-engine').locator('.engine-selector__trigger');
  await expect(trigger).toContainText('Local semantic');
  await trigger.click();
  const dialog = page.getByTestId('engine-selector-dialog');
  await expect(dialog).toBeVisible();

  failRefresh = true;
  // A native dialog makes the underlying form inert to pointer input. Dispatch
  // the form's normal bubbling input event to model the draft refresh that can
  // arrive while this dialog is already open.
  await page.getByTestId('field-context').evaluate((element, value) => {
    const setValue = Object.getOwnPropertyDescriptor(
      element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
      'value',
    )?.set;
    setValue?.call(element, value);
    element.dispatchEvent(new Event('input', { bubbles: true }));
  }, 'refresh while selector is open');
  const retry = dialog.getByRole('button', { name: 'Retry' });
  await expect(dialog.getByRole('alert')).toContainText('Could not refresh choices.');
  await expect(retry).toBeVisible();
  await expect(retry).toBeEnabled();

  failRefresh = false;
  await retry.click();
  await expect.poll(() => requests).toBeGreaterThanOrEqual(3);
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('alert')).toHaveCount(0);
  await expect(dialog.locator('[data-engine-selector-choice="local-semantic"]')).toBeVisible();
});

test('classify uses a full-screen mobile selector with provider tabs and scrollable choices', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const pid = await createProject(page.request, uniqueName('e2e-classify-runner'));
  await importCsv(page.request, pid, 'stories.csv', 'headline\n"Transit service expanded"\n');
  const localChoices = Array.from({ length: 20 }, (_, index) => ({
    choiceId: `local-${index}`,
    label: index === 0 ? 'Local semantic' : `Local classifier ${index}`,
    summary: 'Runs on this computer',
    authoredSelection: { kind: 'engine', engine: index === 0 ? 'local_semantic' : `local-${index}` },
  }));
  const groups: SelectorGroupFixture[] = [
    { id: 'local', label: 'Local', choices: localChoices },
    {
      id: 'api',
      label: 'API models',
      choices: [{
        choiceId: 'openai-gpt-5-mini',
        label: 'GPT-5 mini',
        summary: 'Hosted model',
        description: 'A configured API model.',
        authoredSelection: { kind: 'engine_model', engine: 'llm', model: 'openai/gpt-5-mini' },
      }, {
        choiceId: 'openai-gpt-5-enterprise',
        label: 'GPT-5 enterprise',
        summary: 'Needs workspace setup',
        status: 'needs_setup',
        canRun: false,
        blocker: 'Add a workspace key before this model can run.',
        authoredSelection: { kind: 'engine_model', engine: 'llm', model: 'openai/gpt-5-enterprise' },
      }],
    },
  ];
  await stubActionSelectorChoices(page, pid, ({ actionId, field, params }) => {
    expect(actionId).toBe('map.classify');
    expect(field).toBe('engine');
    const currentChoiceId = params.model === 'openai/gpt-5-enterprise'
      ? 'openai-gpt-5-enterprise'
      : params.engine === 'llm' ? 'openai-gpt-5-mini' : 'local-0';
    return actionSelectorResponse({ projectId: pid, actionId, field, groups, currentChoiceId });
  });
  await page.goto(`/p/${pid}`);
  await openAction(page, 'map.classify');

  const field = page.getByTestId('field-engine');
  const trigger = field.locator('.engine-selector__trigger');
  await expect(trigger).toContainText('Local semantic');
  await trigger.click();
  const dialog = page.getByTestId('engine-selector-dialog');
  await expect(dialog).toHaveJSProperty('open', true);
  const dialogBox = await dialog.boundingBox();
  expect(dialogBox).not.toBeNull();
  expect(Math.round(dialogBox!.width)).toBe(390);
  expect(Math.round(dialogBox!.height)).toBe(844);

  const choices = dialog.locator('.engine-selector__choices');
  await expect.poll(async () => choices.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
  await choices.evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await expect.poll(async () => choices.evaluate((element) => element.scrollTop > 0)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('selector-mobile.png') });

  await dialog.getByRole('button', { name: 'API models' }).click();
  const needsSetup = page.locator('[data-engine-selector-choice="openai-gpt-5-enterprise"]');
  await needsSetup.focus();
  await page.keyboard.press('Enter');
  await expect(dialog.getByRole('button', { name: /Select and set up/ })).toBeVisible();
  await expect(dialog.getByText('Add a workspace key before this model can run.')).toBeVisible();
  await dialog.getByRole('button', { name: /Back/ }).click();
  await page.locator('[data-engine-selector-choice="openai-gpt-5-mini"]').click();
  await expect(dialog.getByRole('button', { name: /Back/ })).toBeFocused();
  await expect(dialog.getByText('A configured API model.')).toBeVisible();
  await dialog.getByRole('button', { name: 'Select', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toContainText('GPT-5 mini');

  // At a genuinely short mobile height, the fixed detail footer remains in
  // the native dialog's viewport instead of moving below its scroll region.
  await page.setViewportSize({ width: 390, height: 420 });
  await trigger.click();
  const shortDialog = page.getByTestId('engine-selector-dialog');
  await shortDialog.locator('[data-engine-selector-choice="openai-gpt-5-mini"]').click();
  const [shortDialogBox, shortFooterBox] = await Promise.all([
    shortDialog.boundingBox(),
    shortDialog.locator('.engine-selector__footer').boundingBox(),
  ]);
  expect(shortDialogBox).not.toBeNull();
  expect(shortFooterBox).not.toBeNull();
  expect(Math.round(shortDialogBox!.height)).toBe(420);
  expect(shortFooterBox!.y + shortFooterBox!.height).toBeLessThanOrEqual(shortDialogBox!.y + shortDialogBox!.height + 1);
});
