import { expect, test } from '@playwright/test';
import { createProject, importCsv, openAction, openProject, uniqueName } from './helpers';

function overflowState(element: HTMLTextAreaElement) {
  return {
    mode: getComputedStyle(element).overflowY,
    hasVerticalOverflow: element.scrollHeight > element.clientHeight,
  };
}

test('Extract instruction hides its initial scrollbar and scrolls only at the height cap', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('extract-instruction-sizing'));
  const sid = await importCsv(page.request, pid, 'stories.csv', 'story\nAlpha\nBeta\n');
  await openProject(page, pid, sid);
  await openAction(page, 'map.extract');

  const instruction = page.getByTestId('field-instruction');
  await expect(instruction).toBeVisible();
  await expect.poll(() => instruction.evaluate(overflowState)).toEqual({
    mode: 'hidden',
    hasVerticalOverflow: false,
  });

  await instruction.fill('Extract names, places, dates, and amounts from this record.\n'.repeat(80));
  await expect.poll(() => instruction.evaluate(overflowState)).toEqual({
    mode: 'auto',
    hasVerticalOverflow: true,
  });
});
