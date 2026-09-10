// AI column grouping uses native Glide group headers. This spec exercises the
// behavior through the canvas header instead of the retired DOM overlay.

import { expect, test, type Page } from '@playwright/test';
import {
  clickGroupHeader,
  clickGroupHeaderRename,
  createProject,
  importCsv,
  openProject,
  openToolbarOverflow,
  runAndWait,
  sheetColumns,
  uniqueName,
  type WireColumn,
} from './helpers';

async function readStoredGroupLabel(page: Page, key: string, runId: number): Promise<string | null> {
  return page.evaluate(
    ({ storageKey, id }) => {
      const raw = localStorage.getItem(storageKey);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as Record<string, { label?: string }>;
      return parsed[String(id)]?.label ?? null;
    },
    { storageKey: key, id: runId },
  );
}

async function openGroupMenu(
  page: Page,
  columns: WireColumn[],
  firstColumnName: string,
  lastColumnName: string,
): Promise<void> {
  const menu = page.getByTestId('native-column-group-menu');
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await clickGroupHeader(page, columns, firstColumnName, lastColumnName);
    if (await menu.isVisible().catch(() => false)) return;
    await page.waitForTimeout(150);
  }
}

test('AI column groups: native shared-run group, rename, persist, and save in views', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-ai-column-groups'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'stories.csv',
    'snippet\n"City hall awarded a paving contract after a closed-door meeting."\n"Transit riders complained about weekend cuts."\n',
  );
  const runId = await runAndWait(page.request, pid, {
    recipe: 'classify',
    model: 'gemini/gemini-2.5-flash',
    sheet_id: sheetId,
    input_columns: ['snippet'],
    context: 'Two short local news snippets.',
    group_label: 'Angle scan',
    fields: [
      {
        name: 'beat',
        type: 'category',
        labels: ['accountability', 'transit', 'other'],
        description: 'best-fit beat',
      },
      {
        name: 'urgency',
        type: 'score',
        description: 'news urgency from 0 to 10',
      },
    ],
    include_confidence: true,
    include_justification: true,
  });

  const columns = await sheetColumns(page.request, pid, sheetId);
  await openProject(page, pid, sheetId);
  const grid = page.getByTestId('grid');
  await expect(grid).toHaveAttribute('data-native-column-groups', 'true');
  await expect(page.locator('.column-groups-bar')).toHaveCount(0);
  await expect
    .poll(() => readStoredGroupLabel(page, `frisket:column-groups:${pid}:${sheetId}`, runId), {
      timeout: 10_000,
    })
    .toBe('Angle scan');

  await clickGroupHeaderRename(page, columns, 'beat_confidence');
  const rename = page.getByTestId('group-rename-input');
  await expect(rename).toBeVisible();
  await rename.fill('Triage signals');
  await rename.press('Enter');
  await expect
    .poll(() => readStoredGroupLabel(page, `frisket:column-groups:${pid}:${sheetId}`, runId), {
      timeout: 5_000,
    })
    .toBe('Triage signals');

  await openGroupMenu(page, columns, 'beat', 'beat_confidence');
  await expect(page.getByTestId('native-column-group-menu')).toContainText('Triage signals');
  await page.getByTestId('native-column-group-show-confidence').uncheck();
  await page.getByTestId('native-column-group-show-justification').uncheck();
  await expect(grid).toHaveAttribute('data-visible-column-names', 'snippet,beat,urgency');

  expect((await sheetColumns(page.request, pid, sheetId)).map((column) => column.name)).toEqual([
    'snippet',
    'beat',
    'beat_justification',
    'urgency',
    'urgency_justification',
    'beat_confidence',
  ]);

  await page.reload();
  await expect(grid).toBeVisible({ timeout: 20_000 });
  await expect(grid).toHaveAttribute('data-visible-column-names', 'snippet,beat,urgency');

  await openToolbarOverflow(page);
  await page.getByTestId('open-saved-views-submenu').click();
  await page.getByTestId('saved-views-overflow-open-panel').click();
  await expect(page.getByTestId('views-panel')).toBeVisible();
  await page.getByTestId('create-saved-view').click();
  await page.getByTestId('view-name-input').fill('Triage layout');
  const viewResponse = page.waitForResponse((res) =>
    res.url().includes(`/api/projects/${pid}/views`) &&
    res.request().method() === 'POST' &&
    res.status() === 200,
  );
  await page.getByTestId('save-view-button').click();
  const view = await (await viewResponse).json();
  expect(view.spec.column_groups).toEqual([
    {
      run_id: runId,
      label: 'Triage signals',
      columns: ['beat', 'beat_justification', 'urgency', 'urgency_justification', 'beat_confidence'],
      show_confidence: false,
      show_justification: false,
    },
  ]);
});
