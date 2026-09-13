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
    }, ...Array.from({ length: 6 }, (_, index) => ({
      choiceId: `document-converter-${index}`,
      label: `Document converter ${index + 1}`,
      summary: 'Document conversion',
      authoredSelection: { kind: 'engine', engine: `document-converter-${index}` },
    }))],
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
  const body = dialog.locator('.engine-selector__body');
  const [groupsBox, choicesBox, detailBox, footerBox] = await Promise.all([
    dialog.locator('.engine-selector__groups').boundingBox(),
    dialog.locator('.engine-selector__choices').boundingBox(),
    dialog.locator('.engine-selector__detail').boundingBox(),
    dialog.locator('.engine-selector__footer').boundingBox(),
  ]);
  expect(groupsBox).not.toBeNull();
  expect(choicesBox).not.toBeNull();
  expect(detailBox).not.toBeNull();
  expect(footerBox).not.toBeNull();
  expect(Math.round(groupsBox!.width)).toBe(160);
  expect(Math.round(choicesBox!.width)).toBe(230);
  // The body has a deliberate 10px dialog gutter; the footer stays aligned to the detail column.
  expect(Math.abs(detailBox!.x + detailBox!.width - (dialogBox!.x + dialogBox!.width - 10))).toBeLessThanOrEqual(2);
  expect(footerBox!.x).toBeGreaterThanOrEqual(detailBox!.x);
  expect(Math.abs(footerBox!.x + footerBox!.width - (detailBox!.x + detailBox!.width))).toBeLessThanOrEqual(2);
  await expect(body).not.toHaveClass(/engine-selector__body--flat/);
  await page.screenshot({ path: testInfo.outputPath('selector-desktop.png') });

  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toBeFocused();

  await trigger.click();
  await dialog.getByRole('searchbox', { name: 'Search Engine' }).fill('Docling');
  await expect(body).toHaveClass(/engine-selector__body--flat/);
  const [flatChoicesBox, flatDetailBox] = await Promise.all([
    dialog.locator('.engine-selector__choices').boundingBox(),
    dialog.locator('.engine-selector__detail').boundingBox(),
  ]);
  expect(flatChoicesBox).not.toBeNull();
  expect(flatDetailBox).not.toBeNull();
  expect(Math.round(flatChoicesBox!.width)).toBe(230);
  expect(Math.abs(flatDetailBox!.x + flatDetailBox!.width - (dialogBox!.x + dialogBox!.width - 10))).toBeLessThanOrEqual(2);
  await page.screenshot({ path: testInfo.outputPath('selector-desktop-search.png') });
  const docling = page.locator('[data-engine-selector-choice="docling"]');
  await docling.focus();
  await page.keyboard.press('Enter');
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toContainText('Docling');
});
