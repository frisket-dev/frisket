import { expect, test } from '@playwright/test';
import { createProject, importCsv, openAction, openProject, uniqueName } from './helpers';

for (const nativeSizing of [true, false]) {
  test(`action prompts grow and retain footer spacing (${nativeSizing ? 'native' : 'fallback'} sizing)`, async ({ page }) => {
    if (!nativeSizing) {
      await page.addInitScript(() => {
        const supports = CSS.supports.bind(CSS);
        CSS.supports = (property: string, value?: string) => property === 'field-sizing'
          ? false : value === undefined ? supports(property) : supports(property, value);
      });
    }
    const pid = await createProject(page.request, uniqueName('prompt-sizing'));
    const sid = await importCsv(page.request, pid, 'stories.csv', 'story\nAlpha\nBeta\n');
    await openProject(page, pid, sid);
    if (!nativeSizing) {
      await page.addStyleTag({ content: '.form-textarea-autogrow { field-sizing: fixed !important; }' });
    }
    await openAction(page, 'map.find');
    const prompt = page.getByLabel('Describe what to look for');
    await prompt.fill('Find references to council meetings.');
    await expect.poll(() => prompt.evaluate((element) => element.scrollHeight - element.clientHeight))
      .toBeLessThanOrEqual(1);
    expect(await prompt.evaluate((element) => element.style.height === '')).toBe(nativeSizing);

    await prompt.fill('Find references to council meetings and their participants.\n'.repeat(80));
    await expect.poll(() => prompt.evaluate((element) => element.scrollHeight > element.clientHeight))
      .toBe(true);
    expect(await prompt.evaluate((element) => element.getBoundingClientRect().height))
      .toBeLessThanOrEqual((page.viewportSize()?.height ?? 900) / 2 + 1);

    // At the end of a long form the footer must not touch the last field.
    const drawer = page.getByTestId('action-drawer');
    const host = drawer.locator('.action-drawer-form-host');
    await host.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    await expect.poll(() => drawer.locator('.action-run-actions').evaluate((footer) => (
      footer.getBoundingClientRect().top - footer.previousElementSibling!.getBoundingClientRect().bottom
    ))).toBeGreaterThanOrEqual(13);

    await prompt.fill('Short again.');
    await expect.poll(() => prompt.evaluate((element) => element.scrollHeight - element.clientHeight))
      .toBeLessThanOrEqual(1);
  });
}
