import { expect, test } from '@playwright/test';
import { createProject, listProjects, openProject, projectIdByName, uniqueName } from './helpers';

// workbench-ia-shell-v1 (Workbench IA increment 9 — the FINAL increment): the
// shell layer. (a) the Home/projects screen (§5A) that replaces the old
// ProjectPicker as the route-home, (b) the combined project switcher menu
// (§5B) off the chrome project-name ▾, and (c) the account menu (§5C) off the
// avatar with an Appearance Light/Dark submenu wiring the existing warm-dark
// theme machinery.
//
// TIER HONESTY RULE binds throughout: the Frisket local tier renders NO
// teams/collaborators/sharing/sign-out chrome. Tier-gated affordances appear
// ONLY when the instance tier provides them (the /api/me 404 local-tier
// convention). The three settings tiers — Account (personal) / Project /
// Workspace (organization) — stay distinct.

// ---------------------------------------------------------------------------
// (a) HOME / PROJECTS SCREEN (§5A)

test('Home renders real project cards with real meta counts and navigates into the workbench', async ({
  page,
}) => {
  const stories = await projectIdByName(page.request, 'Local stories');
  await page.goto('/');

  const home = page.getByTestId('home-screen');
  await expect(home).toBeVisible();

  // The card grid lists the seeded projects with real names.
  const grid = page.getByTestId('project-list');
  await expect(grid).toBeVisible();
  await expect(grid.getByText('Local stories')).toBeVisible();
  await expect(grid.getByText('Tariff impacts')).toBeVisible();

  // A card carries a real meta line — N sheets · N rows · opened … — computed
  // from live data (never fabricated). Local stories has an 8-row sheet.
  const card = page.getByTestId(`project-${stories}`);
  await expect(card).toBeVisible();
  const meta = card.getByTestId(`project-${stories}-meta`);
  await expect(meta).toContainText(/\d+ sheets?/);
  await expect(meta).toContainText(/\d+ rows?/);

  // Clicking a card opens that project into the workbench.
  await card.click();
  await expect(page).toHaveURL(new RegExp(`/p/${stories}(?:/|\\?|$)`));
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });
});

test('+ New project creates a project and opens it', async ({ page }) => {
  const name = uniqueName('e2e-shell-new');
  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();

  await page.getByTestId('home-new-project').click();
  await page.getByTestId('new-project-name').fill(name);
  await page.getByTestId('create-project').click();

  // A fresh project boots straight into its empty workspace.
  await expect(page.getByTestId('import-dropzone')).toBeVisible({ timeout: 20_000 });
  expect(page.url()).toMatch(/\/p\/[^/?#]+/);
});

test('the Grid/List toggle reuses the segmented view switcher and flips the layout', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();

  const grid = page.getByTestId('project-list');
  const switcher = page.getByTestId('home-view-switcher');
  await expect(switcher).toBeVisible();

  await page.getByTestId('home-view-list').click();
  await expect(grid).toHaveAttribute('data-view', 'list');
  await page.getByTestId('home-view-grid').click();
  await expect(grid).toHaveAttribute('data-view', 'grid');
});

test('Archive flags a project (never deletes it): it leaves Recent and is reachable under Archive', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-shell-archive'));
  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();

  const card = page.getByTestId(`project-${pid}`);
  await expect(card).toBeVisible();

  // Archive from the card ⋯ overflow menu.
  await page.getByTestId(`home-card-menu-${pid}`).click();
  await page.getByTestId(`home-card-archive-${pid}`).click();

  // Gone from the default (Recent) scope…
  await expect(page.getByTestId(`project-${pid}`)).toHaveCount(0);

  // …but reachable under the Archive nav scope.
  await page.getByTestId('home-nav-archive').click();
  await expect(page.getByTestId(`project-${pid}`)).toBeVisible();

  // And it is a FLAG, not a deletion — the project still exists server-side.
  const projects = await listProjects(page.request);
  expect(projects.some((p) => p.id === pid)).toBe(true);
});

test('the local tier renders NO teams / Shared-with-me / collaborator chrome on Home', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();

  // Tier-gated nav scopes are absent on the local tier.
  await expect(page.getByTestId('home-nav-shared')).toHaveCount(0);
  await expect(page.getByTestId('home-nav-teams')).toHaveCount(0);
  // No fabricated collaborator avatar stacks on the cards.
  await expect(page.locator('[data-testid^="home-card-collaborators-"]')).toHaveCount(0);
});

// ---------------------------------------------------------------------------
// (b) PROJECT SWITCHER MENU (§5B)

test('the switcher menu opens from the chrome; This-project settings lands on the project route', async ({
  page,
}) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);

  await page.getByTestId('chrome-bar').getByTestId('switch-project').click();
  const menu = page.getByTestId('project-menu');
  await expect(menu).toBeVisible();

  // THIS PROJECT group → Project settings lands on the project settings tier.
  await menu.getByTestId('project-settings-open').click();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/settings/project/general(?:\\?|$)`));
  await expect(page.getByTestId('settings-section-project-general')).toBeVisible();
});

test('the switcher menu recent-projects entry switches projects', async ({ page }) => {
  const stories = await projectIdByName(page.request, 'Local stories');
  const tariff = await projectIdByName(page.request, 'Tariff impacts');
  await openProject(page, stories);

  await page.getByTestId('switch-project').click();
  await expect(page.getByTestId('project-menu')).toBeVisible();
  await page.getByTestId(`menu-project-${tariff}`).click();

  await page.waitForURL(`**/p/${tariff}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('switch-project')).toHaveText(/Tariff impacts/);
});

// ---------------------------------------------------------------------------
// (c) ACCOUNT MENU (§5C)

test('the account menu opens tier-honest and its Appearance submenu flips + persists the theme', async ({
  page,
}) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);

  const avatar = page.getByTestId('chrome-account');
  await avatar.click();
  const menu = page.getByTestId('account-menu');
  await expect(menu).toBeVisible();
  // The avatar carries a focus ring / open state while the menu is open.
  await expect(avatar).toHaveAttribute('aria-expanded', 'true');

  // TIER HONESTY: no Sign out on the local tier (no auth session).
  await expect(menu.getByTestId('account-sign-out')).toHaveCount(0);

  // Appearance submenu wires the EXISTING warm-dark theme machinery: it sets
  // the html data-frisket-theme attribute and persists to the preferences key.
  await menu.getByTestId('account-appearance').click();
  await menu.getByTestId('account-appearance-dark').click();
  await expect(page.locator('html')).toHaveAttribute('data-frisket-theme', 'dark');

  const stored = await page.evaluate(() =>
    window.localStorage.getItem('frisket.personal_preferences.v1'),
  );
  expect(stored ?? '').toContain('"theme":"dark"');

  // Persists across a reload.
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-frisket-theme', 'dark');

  // And back to Light flips the attribute the other way.
  await page.getByTestId('chrome-account').click();
  await page.getByTestId('account-appearance').click();
  await page.getByTestId('account-appearance-light').click();
  await expect(page.locator('html')).toHaveAttribute('data-frisket-theme', 'light');
});

test('the three settings tiers stay distinct and reachable (Account / Project / Workspace)', async ({
  page,
}) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);

  // Account tier (the person) — off the avatar.
  await page.getByTestId('chrome-account').click();
  await page.getByTestId('account-settings').click();
  await expect(page).toHaveURL(/\/settings\/personal\/profile(?:\?|$)/);
  await expect(page.getByTestId('settings-section-personal-profile')).toBeVisible();

  // Project tier (this investigation) — off the project name ▾.
  await openProject(page, pid);
  await page.getByTestId('switch-project').click();
  await page.getByTestId('project-settings-open').click();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/settings/project/general(?:\\?|$)`));

  // Workspace tier — off Home's left nav; a distinct route. In LOCAL mode
  // (this suite) every ORGANIZATION section is dead ("unavailable in local
  // mode"), so the button lands on the workspace-level section that works:
  // personal AI Providers (provider keys + the local AI server URL).
  await page.goto('/');
  await page.getByTestId('home-nav-workspace-settings').click();
  await expect(page).toHaveURL(/\/settings\/personal\/ai-providers(?:\?|$)/);
});

test('Home and workspace share ONE chrome bar (common wrapper, same frame)', async ({ page }) => {
  // Both surfaces render ChromeBarShell — same testid, same computed height,
  // same background; the Home bar can never drift into its own variant again
  // (it had grown 52px/--bg with a different brand tile).
  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();
  const homeBar = page.getByTestId('chrome-bar');
  await expect(homeBar).toBeVisible();
  const homeBox = (await homeBar.boundingBox())!;
  const homeBg = await homeBar.evaluate((el) => getComputedStyle(el).backgroundColor);

  const pid = await createProject(page.request, uniqueName('shell-chrome'));
  await openProject(page, pid);
  const wsBar = page.getByTestId('chrome-bar');
  await expect(wsBar).toBeVisible();
  const wsBox = (await wsBar.boundingBox())!;
  const wsBg = await wsBar.evaluate((el) => getComputedStyle(el).backgroundColor);

  expect(Math.round(homeBox.height)).toBe(Math.round(wsBox.height));
  expect(homeBg).toBe(wsBg);
});
