import { expect, test, type Page } from '@playwright/test';
import { openProject, projectIdByName } from './helpers';

// onboard-replay-banner-v1: the persistent top bar appears ONLY when the active
// mode makes live AI calls impossible. The default cache_mode="replay" is
// cache-FIRST but falls through to a live call on a miss — so it is live-call-
// capable and shows NO banner. Strict replay is the one no-live-calls posture,
// so these tests pin the server into replay_strict via GET /api/config to
// exercise the bar deterministically. With no platform providers configured the
// bar's root cause is "no providers", so it links to the AI-providers page; the
// AI-call-mode explainer itself lives in Settings → Preferences.

async function forceMode(
  page: Page,
  cache_mode: string,
  live_calls_possible: boolean,
): Promise<void> {
  await page.route('**/api/config', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        cache_mode,
        live_calls_possible,
        cache_mode_editable: false,
        email_from_address: null,
        email_from_name: null,
      }),
    }),
  );
}

async function noProvidersConfigured(page: Page): Promise<void> {
  await page.route('**/api/providers', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ schemaVersion: 'frisket.providers.v1', tier: 'local', providers: [] }),
    }),
  );
  await page.route('**/api/org/keys', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) }),
  );
  await page.route('**/api/projects/*/provider-keys', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ providers: [] }),
    }),
  );
}

test('the default replay mode is live-call-capable and shows no banner', async ({ page }) => {
  await forceMode(page, 'replay', true);
  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();
  await expect(page.getByTestId('replay-mode-banner')).toHaveCount(0);
});

test('strict replay shows the top bar on the project picker with no-live-calls copy', async ({
  page,
}) => {
  await forceMode(page, 'replay_strict', false);
  await noProvidersConfigured(page);
  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();

  const banner = page.getByTestId('replay-mode-banner');
  await expect(banner).toBeVisible();
  await expect(banner).toContainText(/no live AI calls|strict replay/i);
});

test('the top bar persists inside an open project workbench under strict replay', async ({
  page,
}) => {
  await forceMode(page, 'replay_strict', false);
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);

  await expect(page.getByTestId('workbench-shell')).toBeVisible();
  await expect(page.getByTestId('replay-mode-banner')).toBeVisible();
});

test('with no providers, the setup link opens project AI provider settings when a project is open', async ({
  page,
}) => {
  await forceMode(page, 'replay_strict', false);
  await noProvidersConfigured(page);
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);

  await page.getByTestId('replay-mode-banner-setup-link').click();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/settings/project/ai-providers(?:\\?|$)`));
  await expect(page.getByTestId('settings-workspace')).toBeVisible();
});

test('with no providers, the setup link opens organization AI provider settings with no project open', async ({
  page,
}) => {
  await forceMode(page, 'replay_strict', false);
  await noProvidersConfigured(page);
  await page.goto('/');
  await page.getByTestId('replay-mode-banner-setup-link').click();
  await expect(page).toHaveURL(/\/settings\/organization\/ai-providers(?:\?|$)/);
  await expect(page.getByTestId('settings-workspace')).toBeVisible();
});

test('the banner collapses but is never permanently dismissible', async ({ page }) => {
  await forceMode(page, 'replay_strict', false);
  await noProvidersConfigured(page);
  await page.goto('/');
  const banner = page.getByTestId('replay-mode-banner');
  await expect(banner).toBeVisible();
  await expect(page.getByTestId('replay-mode-banner-text')).toBeVisible();

  // Collapsing shrinks it to a slim strip — it never fully disappears, and
  // there is no "close forever" control.
  await expect(page.getByTestId('replay-mode-banner-dismiss-forever')).toHaveCount(0);
  await page.getByTestId('replay-mode-banner-toggle').click();
  await expect(banner).toBeVisible();
  await expect(page.getByTestId('replay-mode-banner-text')).toHaveCount(0);

  // A collapse is a per-session UI convenience, not a persisted dismissal:
  // reloading brings the full banner back rather than remembering "hidden".
  await page.reload();
  await expect(page.getByTestId('home-screen')).toBeVisible();
  await expect(page.getByTestId('replay-mode-banner')).toBeVisible();
  await expect(page.getByTestId('replay-mode-banner-text')).toBeVisible();
});

test('the banner does not render when the active mode allows live calls', async ({ page }) => {
  await forceMode(page, 'fresh', true);
  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();
  await expect(page.getByTestId('replay-mode-banner')).toHaveCount(0);
});
