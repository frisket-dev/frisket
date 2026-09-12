import { expect, test } from '@playwright/test';
import { createProject, importCsv, openAction, uniqueName } from './helpers';
import {
  actionSelectorResponse,
  stubActionSelectorChoices,
  type SelectorGroupFixture,
} from './selectorChoicesFixture';

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
      }],
    },
  ];
  await stubActionSelectorChoices(page, pid, ({ actionId, field, params }) => {
    expect(actionId).toBe('map.classify');
    expect(field).toBe('engine');
    const currentChoiceId = params.engine === 'llm' ? 'openai-gpt-5-mini' : 'local-0';
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
  await page.locator('[data-engine-selector-choice="openai-gpt-5-mini"]').click();
  await expect(dialog.getByRole('button', { name: /Back/ })).toBeFocused();
  await expect(dialog.getByText('A configured API model.')).toBeVisible();
  await dialog.getByRole('button', { name: 'Select', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toContainText('GPT-5 mini');
});
