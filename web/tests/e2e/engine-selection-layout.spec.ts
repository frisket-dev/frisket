import { expect, test } from '@playwright/test';
import { createProject, importCsv, openAction, uniqueName } from './helpers';
import {
  actionSelectorResponse,
  stubActionSelectorChoices,
  type SelectorGroupFixture,
} from './selectorChoicesFixture';

test('Markdown uses an anchored native selector that restores trigger focus after dismissal', async ({ page }, testInfo) => {
  const pid = await createProject(page.request, uniqueName('e2e-markdown-engine'));
  await importCsv(page.request, pid, 'documents.csv', 'html\n"<p>Document</p>"\n');
  const groups: SelectorGroupFixture[] = [{
    id: 'models-server',
    label: 'Models server',
    choices: [{
      choiceId: 'docling',
      label: 'Docling',
      summary: 'Document conversion',
      description: 'A model-backed document converter.',
      authoredSelection: { kind: 'engine', engine: 'docling' },
    }],
  }];
  await stubActionSelectorChoices(page, pid, ({ actionId, field, params }) => {
    expect(actionId).toBe('media.to_markdown');
    expect(field).toBe('engine');
    return actionSelectorResponse({
      projectId: pid,
      actionId,
      field,
      groups,
      currentChoiceId: params.engine === 'docling' ? 'docling' : 'markitdown',
      orphanedCurrent: params.engine === 'docling' ? null : {
        choiceId: 'markitdown',
        label: 'MarkItDown',
        summary: 'Saved choice is unavailable',
        status: 'unavailable',
        canAuthor: false,
        canRun: false,
        blocker: 'MarkItDown is not available on this deployment.',
        authoredSelection: { kind: 'engine', engine: 'markitdown' },
      },
    });
  });
  await page.goto(`/p/${pid}`);
  await openAction(page, 'media.to_markdown');

  const field = page.getByTestId('field-engine');
  const trigger = field.locator('.engine-selector__trigger');
  await expect(trigger).toContainText('MarkItDown');

  const [fieldBox, triggerBox] = await Promise.all([field.boundingBox(), trigger.boundingBox()]);
  expect(fieldBox).not.toBeNull();
  expect(triggerBox).not.toBeNull();
  expect(Math.abs(triggerBox!.width - fieldBox!.width)).toBeLessThanOrEqual(2);

  await trigger.click();
  const dialog = page.getByTestId('engine-selector-dialog');
  await expect(dialog).toHaveJSProperty('open', true);
  await expect(dialog.getByRole('searchbox', { name: 'Search Engine' })).toBeFocused();
  const dialogBox = await dialog.boundingBox();
  expect(dialogBox).not.toBeNull();
  expect(dialogBox!.width).toBeGreaterThanOrEqual(758);
  expect(dialogBox!.width).toBeLessThanOrEqual(762);
  expect(dialogBox!.height).toBeLessThanOrEqual(420);
  expect(dialogBox!.x + dialogBox!.width).toBeLessThanOrEqual(triggerBox!.x + triggerBox!.width + 2);
  await page.screenshot({ path: testInfo.outputPath('selector-desktop.png') });

  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toBeFocused();

  await trigger.click();
  await dialog.getByRole('searchbox', { name: 'Search Engine' }).fill('Docling');
  const docling = page.locator('[data-engine-selector-choice="docling"]');
  await docling.focus();
  await page.keyboard.press('Enter');
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toContainText('Docling');
});
