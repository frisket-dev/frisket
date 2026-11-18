import { expect, test, type Page } from '@playwright/test';
import {
  clickGroupHeader,
  clickGroupHeaderRename,
  createProject,
  importCsv,
  openProject,
  openToolbarOverflow,
  primeMapRowsReplies,
  runAndWait,
  sheetColumns,
  typedActionRequest,
  uniqueName,
  type WireColumn,
} from './helpers';
import { prepareOwnModelReplay, REPLAY_MODEL } from './replayModelScenario';

async function readStoredGroupLabel(
  page: Page,
  key: string,
  runId: number,
): Promise<string | null> {
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

function visible(columns: WireColumn[], names: string[]): WireColumn[] {
  const wanted = new Set(names);
  return columns.filter((column) => wanted.has(column.name));
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

test('native Glide AI column groups rename and hide support columns as layout state', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-ai-column-groups-native'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'stories.csv',
    'snippet\n"City hall awarded a paving contract after a closed-door meeting."\n"Transit riders complained about weekend cuts."\n',
  );
  await prepareOwnModelReplay(page.request, pid);
  const action = typedActionRequest('map.classify', {
    sheetId,
    params: {
      source: ['snippet'],
      engine: 'llm',
      model: REPLAY_MODEL,
      context: 'Two short local news snippets.',
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
    },
  });
  primeMapRowsReplies(pid, action, [
    {
      beat: 'accountability',
      beat_justification: 'A contract awarded after a closed-door meeting.',
      urgency: 7,
      urgency_justification: 'Possible procurement irregularity.',
      beat_confidence: 0.9,
    },
    {
      beat: 'transit',
      beat_justification: 'Riders complaining about service cuts.',
      urgency: 4,
      urgency_justification: 'Routine service story.',
      beat_confidence: 0.8,
    },
  ]);
  const runId = await runAndWait(page.request, pid, action);

  const columns = await sheetColumns(page.request, pid, sheetId);
  await openProject(page, pid, sheetId);

  const grid = page.getByTestId('grid');
  await expect(grid).toHaveAttribute('data-native-column-groups', 'true');
  await expect(grid).toHaveAttribute('data-group-header-height', '28');
  await expect(grid).toHaveAttribute(
    'data-visible-column-names',
    'snippet,beat,urgency,beat_justification,urgency_justification,beat_confidence',
  );
  await expect(page.locator('.column-groups-bar')).toHaveCount(0);

  const storageKey = `frisket:column-groups:${pid}:${sheetId}`;
  await expect
    .poll(() => readStoredGroupLabel(page, storageKey, runId), { timeout: 10_000 })
    .toBe('Classify rows');

  await openGroupMenu(page, columns, 'beat', 'beat_confidence');
  const menu = page.getByTestId('native-column-group-menu');
  await expect(menu).toBeVisible();
  await expect(menu).toContainText('Classify rows');
  await expect(page.getByTestId('native-column-group-show-confidence')).toBeChecked();
  await expect(page.getByTestId('native-column-group-show-justification')).toBeChecked();

  await clickGroupHeaderRename(page, columns, 'beat_confidence');
  const rename = page.getByTestId('group-rename-input');
  await expect(rename).toBeVisible();
  await rename.fill('Triage signals');
  await rename.press('Enter');
  await expect
    .poll(() => readStoredGroupLabel(page, storageKey, runId), { timeout: 5_000 })
    .toBe('Triage signals');

  await openGroupMenu(page, columns, 'beat', 'beat_confidence');
  await expect(menu).toBeVisible();
  await expect(menu).toContainText('Triage signals');
  await page.getByTestId('native-column-group-show-confidence').uncheck();
  await page.getByTestId('native-column-group-show-justification').uncheck();
  await expect(grid).toHaveAttribute('data-visible-column-names', 'snippet,beat,urgency');

  const runColumns = await sheetColumns(page.request, pid, sheetId);
  expect(runColumns.map((column) => column.name)).toEqual([
    'snippet',
    'beat',
    'urgency',
    'beat_justification',
    'urgency_justification',
    'beat_confidence',
  ]);

  await page.reload();
  await expect(grid).toBeVisible({ timeout: 20_000 });
  await expect(grid).toHaveAttribute('data-native-column-groups', 'true');
  await expect(grid).toHaveAttribute('data-visible-column-names', 'snippet,beat,urgency');

  await openGroupMenu(page, visible(columns, ['snippet', 'beat', 'urgency']), 'beat', 'urgency');
  await expect(menu).toBeVisible();
  await expect(menu).toContainText('Triage signals');
  await expect(page.getByTestId('native-column-group-show-confidence')).not.toBeChecked();
  await expect(page.getByTestId('native-column-group-show-justification')).not.toBeChecked();

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
  expect(view.spec.presentation.column_groups).toEqual([
    {
      run_id: runId,
      label: 'Triage signals',
      columns: runColumns.slice(1).map((column) => column.id),
      show_confidence: false,
      show_justification: false,
    },
  ]);
});
