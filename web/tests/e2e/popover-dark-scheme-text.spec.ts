// With the OS in dark
// mode, the copilot popover rendered ghost text — invisible message bubbles,
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
// right) and the OS scheme emulated dark, text inside the copilot popover
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

test('copilot popover text stays readable under a dark OS color scheme', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-dark-popover'));
  await importCsv(page.request, pid, 'stories.csv', CSV);
  await page.route(`**/api/projects/${pid}/copilot`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.copilot_reply.v1',
        reply: 'Here is a classifier.',
        needs_import: false,
        proposals: [
          {
            kind: 'map',
            title: 'Classify stories',
            spec: {
              action_kind: 'map.classify',
              authoring_contract_version: 1,
              params: {
                sheet_id: 1,
                input_columns: ['story'],
                model: 'anthropic/claude-haiku-4-5',
                fields: [
                  { name: 'beat', type: 'category', labels: ['a', 'b'] },
                ],
              },
            },
          },
        ],
        cost_usd: null,
      }),
    }),
  );
  await page.goto(`/p/${pid}`);

  await page.getByTestId('chrome-copilot-toggle').click();
  const panel = page.getByTestId('copilot-panel');
  await expect(panel).toBeVisible();
  await page.getByTestId('copilot-input').fill('classify these');
  await page.getByTestId('copilot-send').click();
  await expect(page.getByTestId('copilot-response-markdown')).toBeVisible();

  const textColorOf = (locator: ReturnType<typeof page.locator>) =>
    locator.evaluate((el) => getComputedStyle(el).color);

  const headerColor = await textColorOf(panel.locator('.copilot-head span').first());
  const bubbleColor = await textColorOf(page.getByTestId('copilot-response-markdown'));
  const titleColor = await textColorOf(panel.locator('.copilot-proposal-title').first());

  for (const [what, color] of [
    ['header', headerColor],
    ['assistant bubble', bubbleColor],
    ['proposal title', titleColor],
  ] as const) {
    expect(color, `${what} must not resolve to the UA dark-scheme CanvasText`).not.toBe(
      'rgb(255, 255, 255)',
    );
  }
});
