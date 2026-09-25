// With the OS in dark
// mode, the ask popover rendered ghost text — invisible message bubbles,
// proposal titles, and header — while buttons with explicit colors survived.
//
// Mechanism: native top-layer [popover] elements carry the UA stylesheet's
// `color: CanvasText` (and `background-color: Canvas`). The app never
// declares a `color-scheme`, so under `prefers-color-scheme: dark` the UA
// resolves CanvasText to WHITE, while the app's own CSS paints its light
// background — white-on-white. Playwright defaults to the light scheme,
// which is why every earlier screenshot looked fine.
//
// DONE means: with the APP THEME pinned LIGHT (the preference the bug needs —
// theme=system correctly goes dark under a dark OS, where white text is
// right) and the OS scheme emulated dark, text inside the ask popover
// (a native [popover] element) computes to the app's text color, never the
// UA's white CanvasText. The fix: the :root/theme-attribute pair declares
// `color-scheme` so UA system colors always track the ACTIVE app theme.

import { expect, test } from '@playwright/test';
import { createProject, importCsv, uniqueName } from './helpers';

const CSV = 'story\n"The council approved a contract."\n';

test.use({ colorScheme: 'dark' });

test.beforeEach(async ({ page }) => {
  // Pin the APP theme light while the OS prefers dark — the exact mismatch
  // that produced white CanvasText over the app's white background.
  await page.addInitScript(() => {
    window.localStorage.setItem(
      'frisket.personal_preferences.v1',
      JSON.stringify({ theme: 'light' }),
    );
  });
});

test('ask popover text stays readable under a dark OS color scheme', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-dark-popover'));
  await importCsv(page.request, pid, 'stories.csv', CSV);
  await page.goto(`/p/${pid}`);

  await page.getByTestId('chrome-ask-toggle').click();
  const panel = page.getByTestId('ask-dock');
  await expect(panel).toBeVisible();
  const color = await panel.locator('.ask-header strong').evaluate((el) => getComputedStyle(el).color);
  expect(color).not.toBe('rgb(255, 255, 255)');
  await expect(panel.getByRole('textbox', { name: 'Question', exact: true })).toBeVisible();
});
