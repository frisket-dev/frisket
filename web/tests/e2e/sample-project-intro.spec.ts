import { expect, test, type Page } from '@playwright/test';
import { createProject, uniqueName } from './helpers';

const TELEMETRY_STORAGE_KEY = 'frisket:product-telemetry:v1';
const INTRO_SEEN_STORAGE_KEY = 'frisket.walkthrough.sample-project.intro-seen.v1';
const GUIDE_SEEN_STORAGE_KEY = 'frisket.walkthrough.guide-seen.v1';
const HINT_DISMISSED_STORAGE_KEY = 'frisket.walkthrough.sample-project.hint-dismissed.v1';

async function resetOnboardingStorage(page: Page): Promise<void> {
  await page.addInitScript((keys) => {
    localStorage.setItem(keys.telemetry, JSON.stringify({ schema_version: 1, state: 'disabled' }));
    localStorage.removeItem(keys.introSeen);
    localStorage.removeItem(keys.guideSeen);
    localStorage.removeItem(keys.hintDismissed);
  }, {
    telemetry: TELEMETRY_STORAGE_KEY,
    introSeen: INTRO_SEEN_STORAGE_KEY,
    guideSeen: GUIDE_SEEN_STORAGE_KEY,
    hintDismissed: HINT_DISMISSED_STORAGE_KEY,
  });
}

async function openSampleFromHome(page: Page): Promise<void> {
  await page.goto('/');
  await page.getByRole('button', { name: 'Open the sample project' }).click();
  await expect(page).toHaveURL(/\/p\/[^/]+/, { timeout: 30_000 });
  await expect(page.getByTestId('sample-project-intro')).toBeVisible({ timeout: 30_000 });
}

test.beforeEach(async ({ page }) => {
  await resetOnboardingStorage(page);
});

test('sample setup opens the native introduction with live walkthrough badges and remembers X dismissal', async ({
  page,
}) => {
  await openSampleFromHome(page);

  const intro = page.getByTestId('sample-project-intro');
  await expect(intro.getByTestId('sample-walkthrough-choice-regex-extract')).toContainText('tables');

  await intro.getByRole('button', { name: 'Close introduction' }).click();
  await expect(intro).toHaveCount(0);
  await expect(page.getByTestId('sample-guide-hint')).toBeVisible();
  await page.getByTestId('sample-guide-hint').getByRole('button', { name: 'Got it' }).click();
  await expect(page.getByTestId('sample-guide-hint')).toHaveCount(0);

  await page.reload();
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('sample-project-intro')).toHaveCount(0);
  await expect(page.getByTestId('sample-guide-hint')).toHaveCount(0);
});

test('Escape dismisses the introduction and Guide consumes its hint into the plain chooser', async ({ page }) => {
  await openSampleFromHome(page);

  await page.keyboard.press('Escape');
  await expect(page.getByTestId('sample-project-intro')).toHaveCount(0);
  await expect(page.getByTestId('sample-guide-hint')).toBeVisible();

  await page.getByTestId('chrome-walkthrough').click();
  const chooser = page.getByTestId('walkthrough-chooser');
  await expect(chooser).toBeVisible();
  await expect(chooser.getByTestId('walkthrough-choice-regex-extract')).toContainText('tables');
  await expect(page.getByTestId('sample-guide-hint')).toHaveCount(0);
  await expect(page.getByTestId('sample-project-intro')).toHaveCount(0);
});

test('Poke around dismisses the introduction and an ordinary project never opens it', async ({
  page,
  request,
}) => {
  await openSampleFromHome(page);
  await page.getByTestId('sample-project-intro').getByRole('button', {
    name: 'Poke around first',
    exact: true,
  }).click();
  await expect(page.getByTestId('sample-project-intro')).toHaveCount(0);
  await expect(page.getByTestId('sample-guide-hint')).toBeVisible();

  const name = uniqueName('e2e-ordinary-project-no-sample-intro');
  const projectId = await createProject(request, name);
  try {
    await page.goto(`/p/${projectId}`);
    await expect(page.getByTestId('grid').or(page.getByTestId('import-dropzone')).first()).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.getByTestId('sample-project-intro')).toHaveCount(0);
  } finally {
    const deleted = await request.delete(`/api/projects/${projectId}`, {
      data: { confirm_name: name },
    });
    expect(deleted.ok()).toBeTruthy();
  }
});
