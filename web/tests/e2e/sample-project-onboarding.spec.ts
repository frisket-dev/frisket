// "Try the sample project" onboarding: the ProjectPicker offers a one-click
// sample that creates a REAL server-seeded project (no ?mock=1 in-memory demo),
// lands the user in it, and provides raw Dispatches + Contracts content for the
// guided investigations without precomputed analysis columns. The same ready
// project also includes searchable lawsuit PDFs and local council audio.
//
// onboard2-sample-project-idempotent-v1: both tests below share one workspace
// and both exercise the same "Sample project" resource, so this file runs
// serially — parallel workers hitting the same named project concurrently
// would be a genuine (out-of-scope) race, not the repeat-click idempotency
// this task covers.
import { test, expect } from '@playwright/test';
import { listSheets, sheetColumns, sheetData } from './helpers';

test.describe.configure({ mode: 'serial' });

// This spec verifies sample content and reuse; intro interactions have their
// own browser coverage in sample-project-intro.spec.ts.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('frisket.walkthrough.sample-project.intro-seen.v1', 'true');
    localStorage.setItem('frisket.walkthrough.sample-project.hint-dismissed.v1', 'true');
  });
});

test('sample-project button seeds raw Dispatches and Contracts sheets', async ({
  page,
  request,
}) => {
  await page.goto('/');

  const button = page.getByTestId('try-sample-project');
  await expect(button).toBeVisible();
  await button.click();

  // Lands the user in a real project route (/p/{pid}), not an in-memory /mock demo.
  await expect(page).toHaveURL(/\/p\/[^/]+/, { timeout: 30_000 });
  await expect(
    page.getByTestId('grid').or(page.getByTestId('import-dropzone')).first(),
  ).toBeVisible({ timeout: 30_000 });

  const pid = new URL(page.url()).pathname.split('/')[2];
  expect(pid).toBeTruthy();
  expect(pid).not.toBe('mock');

  // The project is REAL: it appears in the workspace project listing.
  const projectsRes = await request.get('/api/projects');
  expect(projectsRes.ok()).toBeTruthy();
  const projects = (await projectsRes.json()) as Array<{ id: string; name: string }>;
  expect(projects.some((p) => p.id === pid)).toBeTruthy();

  // Real rows exist in the seeded sheet.
  const sheets = await listSheets(request, pid);
  expect(sheets.length).toBeGreaterThan(0);
  const dispatches = sheets.find((s) => s.name === 'Dispatches');
  const contracts = sheets.find((s) => s.name === 'Contracts');
  const docket = sheets.find((s) => s.name === 'Court docket');
  const filings = sheets.find((s) => s.name === 'Court filings');
  const meetings = sheets.find((s) => s.name === 'Council meetings');
  const audio = sheets.find((s) => s.name === 'Council audio');
  const multilingual = sheets.find((s) => s.name === 'Multilingual names');
  const agencyPayments = sheets.find((s) => s.name === 'Agency payments');
  const smallModelLab = sheets.find((s) => s.name === 'Small model lab');
  expect(dispatches?.rows).toBeGreaterThan(0);
  expect(contracts?.rows).toBeGreaterThan(0);
  expect(docket?.rows).toBe(6);
  expect(filings?.rows).toBe(6);
  expect(meetings?.rows).toBe(2);
  expect(audio?.rows).toBe(2);
  expect(multilingual?.rows).toBe(10);
  expect(agencyPayments?.rows).toBe(16);
  expect(smallModelLab?.rows).toBe(6);

  const columns = await sheetColumns(request, pid, dispatches!.id);
  const names = new Set(columns.map((column) => column.name));
  for (const required of ['record_id', 'headline', 'published_at', 'canonical_url', 'story']) {
    expect(names.has(required), `missing raw column ${required}`).toBeTruthy();
  }
  for (const generated of ['summary', 'topic', 'relevance', 'word_count']) {
    expect(names.has(generated), `sample must not precompute ${generated}`).toBeFalsy();
  }

  const data = await sheetData(request, pid, dispatches!.id, 0, 24);
  expect(data.rows.length).toBeGreaterThan(0);
  const story = columns.find((column) => column.name === 'story')!;
  const storyLengths = data.rows.map((row) => String(row.cells[String(story.id)] ?? '').length);
  expect(Math.min(...storyLengths)).toBeLessThan(200);
  expect(Math.max(...storyLengths)).toBeGreaterThan(1_000);

  await page.getByTestId('chrome-walkthrough').click();
  const chooser = page.getByTestId('walkthrough-chooser');
  await expect(chooser).toBeVisible();
  const chooserBox = await chooser.boundingBox();
  const viewport = page.viewportSize();
  expect(chooserBox).toBeTruthy();
  expect(viewport).toBeTruthy();
  expect(Math.abs(chooserBox!.x + chooserBox!.width / 2 - viewport!.width / 2)).toBeLessThan(2);
  await expect(page.locator('[data-testid^="walkthrough-choice-"]')).toHaveCount(8);
  await page.getByRole('button', { name: 'Close walkthrough chooser' }).click();
});

test('repeat clicks reuse the existing sample project instead of duplicating it', async ({
  page,
  request,
}) => {
  await page.goto('/');
  const button = page.getByTestId('try-sample-project');
  await expect(button).toBeVisible();
  await button.click();

  await expect(page).toHaveURL(/\/p\/[^/]+/, { timeout: 30_000 });
  await expect(
    page.getByTestId('grid').or(page.getByTestId('import-dropzone')).first(),
  ).toBeVisible({ timeout: 30_000 });
  const firstPid = new URL(page.url()).pathname.split('/')[2];

  const projectsAfterFirst = (await (await request.get('/api/projects')).json()) as Array<{
    id: string;
    name: string;
  }>;
  const sampleEntriesAfterFirst = projectsAfterFirst.filter((p) => p.name === 'Sample project');
  // Depending on test order this may be the first-ever sample project or a
  // reuse of one created by the sibling test above — either way there must
  // be exactly one "Sample project" entry, never more.
  expect(sampleEntriesAfterFirst.length).toBe(1);

  const sheetsAfterFirst = await listSheets(request, firstPid);
  const dispatchesAfterFirst = sheetsAfterFirst.filter((s) => s.name === 'Dispatches');
  expect(dispatchesAfterFirst.length).toBe(1);
  expect(sheetsAfterFirst.filter((s) => s.name === 'Contracts')).toHaveLength(1);
  expect(sheetsAfterFirst.filter((s) => s.name === 'Court docket')).toHaveLength(1);
  expect(sheetsAfterFirst.filter((s) => s.name === 'Court filings')).toHaveLength(1);
  expect(sheetsAfterFirst.filter((s) => s.name === 'Council meetings')).toHaveLength(1);
  expect(sheetsAfterFirst.filter((s) => s.name === 'Council audio')).toHaveLength(1);
  expect(sheetsAfterFirst.filter((s) => s.name === 'Agency payments')).toHaveLength(1);
  expect(sheetsAfterFirst.filter((s) => s.name === 'Small model lab')).toHaveLength(1);
  const rowsAfterFirst = dispatchesAfterFirst[0].rows;
  expect(rowsAfterFirst).toBeGreaterThan(0);

  // Return to the picker (brand-home link) and click the sample button again.
  await page.getByTestId('brand-home').click();
  await expect(page.getByTestId('home-screen')).toBeVisible();
  await expect(button).toBeVisible();
  await button.click();

  await expect(page).toHaveURL(/\/p\/[^/]+/, { timeout: 30_000 });
  await expect(
    page.getByTestId('grid').or(page.getByTestId('import-dropzone')).first(),
  ).toBeVisible({ timeout: 30_000 });
  const secondPid = new URL(page.url()).pathname.split('/')[2];

  // Idempotent: the same project is reopened, not a new one created.
  expect(secondPid).toBe(firstPid);

  const projectsAfterSecond = (await (await request.get('/api/projects')).json()) as Array<{
    id: string;
    name: string;
  }>;
  const sampleEntriesAfterSecond = projectsAfterSecond.filter((p) => p.name === 'Sample project');
  expect(sampleEntriesAfterSecond.length).toBe(1);

  // No second content sheet was imported into the reused project either.
  const sheetsAfterSecond = await listSheets(request, secondPid);
  const dispatchesAfterSecond = sheetsAfterSecond.filter((s) => s.name === 'Dispatches');
  expect(dispatchesAfterSecond.length).toBe(1);
  expect(sheetsAfterSecond.filter((s) => s.name === 'Contracts')).toHaveLength(1);
  expect(sheetsAfterSecond.filter((s) => s.name === 'Court docket')).toHaveLength(1);
  expect(sheetsAfterSecond.filter((s) => s.name === 'Court filings')).toHaveLength(1);
  expect(sheetsAfterSecond.filter((s) => s.name === 'Council meetings')).toHaveLength(1);
  expect(sheetsAfterSecond.filter((s) => s.name === 'Council audio')).toHaveLength(1);
  expect(sheetsAfterSecond.filter((s) => s.name === 'Agency payments')).toHaveLength(1);
  expect(sheetsAfterSecond.filter((s) => s.name === 'Small model lab')).toHaveLength(1);
  expect(dispatchesAfterSecond[0].id).toBe(dispatchesAfterFirst[0].id);
  expect(dispatchesAfterSecond[0].rows).toBe(rowsAfterFirst);
});
