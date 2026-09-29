import { expect, test } from '@playwright/test';
import { createProject, importCsv, openAction, openProject, uniqueName } from './helpers';

function overflowState(element: HTMLTextAreaElement) {
  return {
    mode: getComputedStyle(element).overflowY,
    hasVerticalOverflow: element.scrollHeight > element.clientHeight,
  };
}

for (const nativeSizing of [true, false]) {
  test(`Extract instruction follows viewport height caps (${nativeSizing ? 'native' : 'fallback'} sizing)`, async ({ page }) => {
    if (!nativeSizing) {
      await page.addInitScript(() => {
        const supports = CSS.supports.bind(CSS);
        CSS.supports = (property: string, value?: string) => property === 'field-sizing'
          ? false : value === undefined ? supports(property) : supports(property, value);
      });
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    const pid = await createProject(page.request, uniqueName('extract-instruction-sizing'));
    const sid = await importCsv(page.request, pid, 'stories.csv', 'story\nAlpha\nBeta\n');
    await openProject(page, pid, sid);
    if (!nativeSizing) {
      await page.addStyleTag({ content: '.form-textarea-autogrow { field-sizing: fixed !important; }' });
    }
    await openAction(page, 'map.extract');

    const instruction = page.getByTestId('field-instruction');
    await expect(instruction).toBeVisible();
    await expect.poll(() => instruction.evaluate(overflowState)).toEqual({
      mode: 'auto',
      hasVerticalOverflow: false,
    });

    await instruction.fill('Read.\n'.repeat(10));
    await expect.poll(() => instruction.evaluate(overflowState)).toEqual({
      mode: 'auto',
      hasVerticalOverflow: false,
    });

    await page.setViewportSize({ width: 1280, height: 360 });
    await expect.poll(() => instruction.evaluate(overflowState)).toEqual({
      mode: 'auto',
      hasVerticalOverflow: true,
    });

    await page.setViewportSize({ width: 1280, height: 900 });
    await expect.poll(() => instruction.evaluate(overflowState)).toEqual({
      mode: 'auto',
      hasVerticalOverflow: false,
    });
  });
}
