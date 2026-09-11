import { expect, test, type Page } from '@playwright/test';
import { createProject, uniqueName } from './helpers';

test.describe.configure({ mode: 'serial' });

async function openSampleFromHome(page: Page): Promise<void> {
  await page.goto('/');
  await page.getByTestId('try-sample-project').click();
  await expect(page).toHaveURL(/\/p\/[^/]+/, { timeout: 30_000 });
  await expect(page.getByTestId('sample-project-intro')).toBeVisible({ timeout: 30_000 });
}

test.beforeEach(async ({ page }) => {
  // Each test gets fresh browser storage. Do not reset intro flags on reload:
  // persistence across that reload is part of the acceptance behavior.
  await page.addInitScript(() => {
    localStorage.setItem('frisket:product-telemetry:v1', JSON.stringify({ schema_version: 1, state: 'disabled' }));
  });
});

test('guided Home entry opens the existing chooser without the introduction', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('home-sample-hero')).toBeVisible();
  await page.getByRole('button', { name: 'See the walkthroughs' }).click();
  await expect(page).toHaveURL(/\/p\/[^/]+/, { timeout: 30_000 });
  const chooser = page.getByTestId('walkthrough-chooser');
  await expect(chooser).toBeVisible();
  await expect(chooser.getByTestId('walkthrough-choice-regex-extract')).toContainText('Regex');
  await expect(page.getByTestId('sample-project-intro')).toHaveCount(0);
  await page.reload();
  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('sample-project-intro')).toHaveCount(0);
});

test('sample setup opens the native introduction with live walkthrough badges and remembers X dismissal', async ({
  page,
}) => {
  await openSampleFromHome(page);

  const intro = page.getByTestId('sample-project-intro');
  await expect(intro.getByTestId('sample-walkthrough-choice-regex-extract')).toContainText('Regex');
  expect(await intro.evaluate((element) => element.matches(':modal'))).toBe(true);
  // Native dialogs may let Tab reach browser chrome; the application behind
  // the dialog must remain inert and cannot take focus.
  await page.getByTestId('chrome-walkthrough').evaluate((button) => button.focus());
  expect(await intro.evaluate((element) => element.contains(document.activeElement))).toBe(true);
  await page.setViewportSize({ width: 390, height: 650 });
  const box = await intro.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.y).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(390);
  expect(box!.y + box!.height).toBeLessThanOrEqual(650);
  await intro.getByTestId('sample-walkthrough-choice-local-model-lab').scrollIntoViewIfNeeded();
  await expect(intro.getByTestId('sample-walkthrough-choice-local-model-lab')).toBeVisible();

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
  await expect(chooser.getByTestId('walkthrough-choice-regex-extract')).toContainText('Regex');
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
    await expect(page.getByTestId('sample-guide-hint')).toHaveCount(0);
  } finally {
    // Unmount the workspace before deleting its backing database.
    await page.goto('about:blank');
    const deleted = await request.delete(`/api/projects/${projectId}`, {
      data: { confirm_name: name },
    });
    expect(deleted.ok()).toBeTruthy();
  }
});

test('clicking the scrim dismisses the introduction like Poke around', async ({ page }) => {
  await openSampleFromHome(page);
  await page.mouse.click(2, 2);
  await expect(page.getByTestId('sample-project-intro')).toHaveCount(0);
  await expect(page.getByTestId('sample-guide-hint')).toBeVisible();
});

test('a walkthrough starts directly from the introduction after the modal closes', async ({ page }) => {
  await openSampleFromHome(page);
  await page.getByTestId('sample-walkthrough-choice-regex-extract').click();
  await expect(page.getByTestId('sample-project-intro')).toHaveCount(0);
  await expect(page.getByTestId('walkthrough-card')).toBeVisible();
  await expect(page.getByTestId('sample-guide-hint')).toHaveCount(0);
  await page.getByRole('button', { name: 'Pause walkthrough' }).click();
  await expect(page.getByTestId('chrome-walkthrough-resume')).toBeVisible();
});
