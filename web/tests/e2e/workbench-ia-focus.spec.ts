import { expect, test, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openImportWorkspace,
  openPalette,
  openProject,
  projectIdByName,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

// workbench-ia-focus-v1 (Workbench IA increment 8): the ⌘K palette upgrade
// (BEST MATCH / ACTIONS / GO TO / SEARCH + live filter + arrow-key nav +
// configure-first ↵), the Copilot Focus popover, and the FULL retirement of the
// left sidebar (Search → palette, Copilot → popover, Settings → project menu +
// palette command, Sidebar.tsx deleted).

const STORY_CSV = [
  'story,url',
  '"Contract hearing delayed",https://example.com/a',
  '"Bridge closure reroutes buses",https://example.com/b',
].join('\n');

async function seedStories(page: Page): Promise<{ pid: string; sheetId: number }> {
  const pid = await createProject(page.request, uniqueName('e2e-focus'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', STORY_CSV);
  await openProject(page, pid, sheetId);
  return { pid, sheetId };
}

test('⌘K and the chrome Command pill both open the palette; the query live-filters ACTIONS', async ({
  page,
}) => {
  await seedStories(page);
  const palette = page.getByTestId('workbench-region-commandPalette');

  // Opened by the chrome pill…
  await page.getByTestId('chrome-command-pill').click();
  await expect(palette).toBeVisible();
  await expect(palette).toHaveAttribute('data-host', 'commandPalette');
  const unfiltered = await palette.getByTestId('palette-action-item').count();
  expect(unfiltered).toBeGreaterThan(1);

  // …and closed with Esc.
  await page.keyboard.press('Escape');
  await expect(palette).toBeHidden();

  // …and by ⌘K (the primary chord, §12), which is a TOGGLE.
  await openPalette(page);

  // Live text filtering narrows the ACTIONS list as typed.
  await page.getByTestId('command-palette-input').fill('summarize');
  const filtered = palette.getByTestId('palette-action-item');
  await expect(filtered.filter({ hasText: /summarize/i })).toHaveCount(1);
  expect(await filtered.count()).toBeLessThan(unfiltered);

  // The key legend is present (the spec's production-work footer).
  await expect(palette.getByTestId('palette-key-legend')).toBeVisible();

  // ⌘K again toggles it closed.
  await page.keyboard.press('ControlOrMeta+k');
  await expect(palette).toBeHidden();
});

test('↑↓ + ↵ navigates via the GO TO section', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-focus-goto'));
  const first = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  const second = await importCsv(page.request, pid, 'zephyr.csv', 'name\nZephyr\n');
  await openProject(page, pid, first);

  const palette = await openPalette(page);

  // A sheet-name query surfaces its GO TO row (action names never match it).
  await page.getByTestId('command-palette-input').fill('zephyr');
  const gotoRow = palette.getByTestId('palette-goto-item').filter({ hasText: /zephyr/i });
  await expect(gotoRow).toHaveCount(1);
  await expect(gotoRow).toHaveAttribute('data-sheet-id', String(second));

  // Exercise arrow-key movement, then ↵ to navigate — the GO TO row (index 0)
  // is highlighted; ArrowDown/ArrowUp return to it before Enter.
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowUp');
  await expect(gotoRow).toHaveAttribute('data-active', 'true');
  await page.keyboard.press('Enter');

  await expect(palette).toBeHidden();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${second}$`));
  await expect(page.getByTestId(`workbench-mainView-tab-${second}`)).toHaveClass(/active/);
});

test('↵ on an ACTION opens the configure-first drawer with ZERO run POSTs (§12)', async ({
  page,
}) => {
  const { pid } = await seedStories(page);

  // Count any run POSTs — a palette ↵ must NEVER trigger one.
  let runPosts = 0;
  page.on('request', (request) => {
    if (
      request.method() === 'POST' &&
      request.url().includes(`/api/projects/${pid}/actions/v1/run`)
    ) {
      runPosts += 1;
    }
  });

  const palette = await openPalette(page);
  await page.getByTestId('command-palette-input').fill('summarize');

  // The first matching row is the Summarize action; ↵ opens the drawer.
  const actionRow = palette.getByTestId('palette-action-item').filter({ hasText: /summarize/i }).first();
  await expect(actionRow).toBeVisible();
  await actionRow.hover();
  await page.keyboard.press('Enter');

  const drawer = page.getByTestId('action-drawer');
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId('action-form-title')).toContainText(/summarize/i);
  await expect(palette).toBeHidden();

  // Give any (forbidden) run a chance to fire, then assert none did.
  await page.waitForTimeout(400);
  expect(runPosts).toBe(0);
});

test('BEST MATCH ranks a file/link-column action on a file-column sheet', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-focus-best'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'docs.csv',
    'doc_url\nhttps://example.com/one.pdf\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const docUrl = columns.find((c) => c.name === 'doc_url')!;
  // A file/link column: its media/fetch actions must rank in BEST MATCH.
  await setColumnType(page.request, pid, docUrl.id, 'link');
  await openProject(page, pid, sheetId);

  await page.getByTestId('chrome-command-pill').click();
  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();

  const bestMatch = palette.getByTestId('palette-best-match-item');
  await expect(bestMatch.first()).toBeVisible();
  // The top best-match is pre-bound to the file/link column (ranked first) and
  // carries the '↵ run' best-match chip.
  await expect(bestMatch.first()).toHaveAttribute('data-source-column', 'doc_url');
  await expect(palette.getByTestId('palette-best-match-chip').first()).toBeVisible();
});

test('the SEARCH section finds a seeded row and navigates to it', async ({ page }) => {
  // Seeded 'Local stories' has FTS-indexed content ("listeria" in one snippet).
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);

  const palette = await openPalette(page);

  await page.getByTestId('command-palette-input').fill('listeria');
  const results = palette.getByTestId('search-results');
  await expect(results).toBeVisible();
  await expect(results.locator('.search-group-label').first()).toHaveText('stories');
  const hit = palette.getByTestId('search-hit').first();
  await expect(hit).toContainText('listeria');

  await hit.click();
  await expect(palette).toBeHidden();
  await expect(page.locator('.sheet-title')).toHaveText('stories');
});

test('the ✧ chrome toggle opens the Copilot popover (never a resident column) with a proposal card', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-focus-copilot'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', STORY_CSV);
  await page.route(`**/api/projects/${pid}/copilot`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reply: 'I can add a news beat classifier.',
        needs_import: false,
        proposals: [
          {
            kind: 'map',
            title: 'Add news beat classifier',
            spec: {
              action_kind: 'map.classify',
              authoring_contract_version: 1,
              params: {
                sheet_id: sheetId,
                input_columns: ['story'],
                model: 'anthropic/claude-haiku-4-5',
                context: 'Classify each story by news beat.',
                fields: [{ name: 'beat', type: 'category', labels: ['transit', 'money', 'other'] }],
              },
            },
          },
        ],
      }),
    });
  });
  await openProject(page, pid, sheetId);

  // Closed by default; the ✧ chrome toggle summons the popover.
  await expect(page.getByTestId('copilot-popover')).toHaveCount(0);
  await page.getByTestId('chrome-copilot-toggle').click();
  const popover = page.getByTestId('copilot-popover');
  await expect(popover).toBeVisible();
  await expect(popover.getByTestId('copilot-panel')).toBeVisible();

  // It is a floating overlay, not a resident sidebar column: the left sidebar
  // region does not exist, and the popover is fixed-positioned bottom-right.
  await expect(page.getByTestId('workbench-region-leftSidebar')).toHaveCount(0);
  const box = await popover.boundingBox();
  const viewport = page.viewportSize()!;
  expect(box).not.toBeNull();
  expect(box!.x + box!.width).toBeGreaterThan(viewport.width / 2);
  expect(box!.y + box!.height).toBeGreaterThan(viewport.height / 2);

  // A message yields a runnable proposal card.
  await popover.getByTestId('copilot-input').fill('Classify each story by news beat.');
  await popover.getByTestId('copilot-send').click();
  const proposal = popover.getByTestId('copilot-proposal').first();
  await expect(proposal).toBeVisible({ timeout: 30_000 });
  await expect(proposal.getByTestId('copilot-run')).toBeVisible();

  // Toggling ✧ again dismisses the popover.
  await page.getByTestId('chrome-copilot-toggle').click();
  await expect(page.getByTestId('copilot-popover')).toHaveCount(0);
});

test('the left sidebar is fully retired — no sidebar, no resident Search/Copilot', async ({
  page,
}) => {
  await seedStories(page);

  await expect(page.locator('.sidebar')).toHaveCount(0);
  await expect(page.getByTestId('workbench-region-leftSidebar')).toHaveCount(0);
  // The retired sidebar affordances are gone: the standalone Search trigger and
  // the resident Copilot collapsed trigger no longer exist.
  await expect(page.getByTestId('open-search')).toHaveCount(0);
  await expect(page.getByTestId('copilot-toggle')).toHaveCount(0);
});

test('Import and Settings are reachable at their re-homes', async ({ page }) => {
  const { pid } = await seedStories(page);

  // Import remains reachable through its explicit chrome command.
  await openImportWorkspace(page);
  await expect(page.getByTestId('import-workspace-dialog')).toBeVisible();
  await page.getByTestId('import-workspace-close').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();

  // Settings re-home 1: the project ▾ menu entry.
  await page.getByTestId('switch-project').click();
  await expect(page.getByTestId('project-menu').getByTestId('project-settings-open')).toBeVisible();
  await page.keyboard.press('Escape');

  // Settings re-home 2: a palette command that routes to Project → General.
  const palette = await openPalette(page);
  const settingsCommand = palette.getByTestId('workbench-command-frisket-core-command-open-settings');
  await expect(settingsCommand).toBeVisible();
  await settingsCommand.click();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/settings/project/general(?:\\?|$)`));
  await expect(page.getByTestId('settings-section-project-general')).toBeVisible();
});
