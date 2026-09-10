// Local-edition /admin route (affordance walk #2, finding F): routes.ts parses
// /admin as an admin route regardless of edition posture, but openRoutes.ts's
// LOCAL_ROUTES deliberately excludes admin-* routes in local posture (only
// TEAM_ROUTES, gated on capabilities.identity, contribute them). Before this
// fix, App.tsx's admin case fell through to openHome() with no navigation --
// the URL stayed /admin while Home silently rendered, reading as a broken
// route rather than an intentional edition gate. This suite boots the 'local'
// edition (playwright.config.ts; see settings-project-secrets-plugins.spec.ts
// for the same posture note), where admin is never wired up.

import { expect, test } from '@playwright/test';

test('/admin in local posture shows an Admin-unavailable card, not Home', async ({ page }) => {
  await page.goto('/admin');

  await expect(page.getByTestId('admin-unavailable')).toBeVisible();
  await expect(page.getByTestId('admin-unavailable')).toContainText(
    "Admin isn't available in local mode.",
  );

  // Never silently falls back to rendering Home in its place.
  await expect(page.getByTestId('home-screen')).toHaveCount(0);

  // The card offers a real way out: back to the project picker.
  await page.getByTestId('admin-unavailable-home-link').click();
  await expect(page.getByTestId('home-screen')).toBeVisible();
});
