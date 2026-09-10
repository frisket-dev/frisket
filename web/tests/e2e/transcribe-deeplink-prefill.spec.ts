import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import {
  clickHeaderMenu,
  createProject,
  selectRow,
  sheetColumns,
  uniqueName,
} from './helpers';
import { seedTranscriptStatusSheet } from './transcriptStatusFixtures';

// The deep link this spec drives is the header ⋯ menu's PRE-EXISTING
// "Actions on this column" seam (App.tsx onColumnAction ->
// runActionFromSurface(template.kind, column.name), gridMenus.tsx's
// header-menu-action-${kind}). This spec deliberately exercises the shared
// launch-identity mechanics without depending on another entry point.
//
// Three assertions in one flow:
//   1. deep-link transcribe from column A -> prefilled with A.
//   2. re-launch (same kind, DIFFERENT column B) -> FRESH state: B prefilled,
//      A's dirty edit gone (the ActionForm key folds in launch identity,
//      ActionPanel.tsx's drawer render + :2129-2143's lazy reducer init).
//   3. an unrelated same-launch grid refresh preserves B's valid dirty edit,
//      while an EXPLICIT repeated B click is a new launch and resets it.
test('the transcribe deep link resets every explicit launch while a same-launch grid refresh preserves valid dirty state', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-transcribe-deeplink'));
  const { sheetId } = seedTranscriptStatusSheet(pid, [
    { name: 'media_a', status: 'missing' },
    { name: 'media_b', status: 'missing' },
  ]);

  await page.goto(`/p/${pid}/s/${sheetId}`);
  const columns = await sheetColumns(page.request, pid, sheetId);

  // 1. Column A's deep link opens the transcribe form prefilled with A.
  await clickHeaderMenu(page, columns, 'media_a');
  await page.getByTestId('header-menu-action-media.transcribe').click();

  const drawer = page.getByTestId('action-drawer');
  await expect(drawer).toBeVisible();
  await expect(page.getByTestId('field-source')).toHaveValue('media_a');
  await expect(page.getByTestId('field-output-text')).toHaveValue('transcript');

  // Dirty-edit column A's form.
  await page.getByTestId('field-output-text').fill('dirty-a-edit');
  await expect(page.getByTestId('field-output-text')).toHaveValue('dirty-a-edit');

  // 2. Column B's deep link: SAME kind ('media.transcribe'), DIFFERENT column ->
  // the launch identity differs -> the form resets, not a stale copy of A's
  // edit silently ignoring B.
  await clickHeaderMenu(page, columns, 'media_b');
  await page.getByTestId('header-menu-action-media.transcribe').click();
  await expect(page.getByTestId('field-source')).toHaveValue('media_b');
  await expect(page.getByTestId('field-output-text')).toHaveValue('transcript');
  await expect(page.getByTestId('field-output-text')).not.toHaveValue('dirty-a-edit');

  // Dirty-edit column B's form.
  await page.getByTestId('field-output-text').fill('dirty-b-edit');
  await expect(page.getByTestId('field-output-text')).toHaveValue('dirty-b-edit');

  // 3. Grid selection is a background same-launch refresh: it must not reset
  // unrelated valid edits while this launch remains mounted.
  await selectRow(page, 0);
  await expect(page.getByTestId('field-output-text')).toHaveValue('dirty-b-edit');

  // Clicking the SAME launcher again is still an explicit user launch. It gets
  // a fresh launchId and resets every initial/draft choice atomically.
  await clickHeaderMenu(page, columns, 'media_b');
  await page.getByTestId('header-menu-action-media.transcribe').click();
  await expect(drawer).toBeVisible();
  await expect(page.getByTestId('field-source')).toHaveValue('media_b');
  await expect(page.getByTestId('field-output-text')).toHaveValue('transcript');
  await expect(page.getByTestId('field-output-text')).not.toHaveValue('dirty-b-edit');
});

const REPO_ROOT =
  path.basename(process.cwd()) === 'web' ? path.resolve(process.cwd(), '..') : process.cwd();

function mutateColumn(
  pid: string,
  sheetId: number,
  columnId: string,
  mutation: 'rename' | 'delete',
  renamedTo?: string,
): void {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = String.raw`
import sys
from pathlib import Path

from frisket.engine.store import Project

workspace, pid, sheet_id, column_id, mutation, renamed_to = sys.argv[1:]
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    if mutation == "rename":
        project.db.execute(
            "UPDATE columns SET name=? WHERE sheet_id=? AND id=?",
            (renamed_to, int(sheet_id), int(column_id)),
        )
    else:
        project.db.execute(
            "DELETE FROM columns WHERE sheet_id=? AND id=?",
            (int(sheet_id), int(column_id)),
        )
    project.db.commit()
finally:
    project.close()
`;
  execFileSync(
    'uv',
    [
      'run',
      'python',
      '-c',
      script,
      workspace,
      pid,
      String(sheetId),
      columnId,
      mutation,
      renamedTo ?? '',
    ],
    { cwd: REPO_ROOT, encoding: 'utf8', timeout: 120_000 },
  );
}

async function gateNextSheetList(
  page: import('@playwright/test').Page,
  pid: string,
): Promise<{ started: Promise<void>; release(): void }> {
  let markStarted!: () => void;
  let release!: () => void;
  const started = new Promise<void>((resolve) => {
    markStarted = resolve;
  });
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(
    `**/api/projects/${pid}/sheets`,
    async (route) => {
      markStarted();
      await released;
      await route.continue();
    },
    { times: 1 },
  );
  return { started, release };
}

test('same-launch column refresh follows stable ids across rename and clears a deleted source without first-column fallback', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-transcribe-stable-source'));
  const { sheetId } = seedTranscriptStatusSheet(pid, [
    { name: 'media_a', status: 'missing' },
    { name: 'media_b', status: 'missing' },
  ]);
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible();
  const initialColumns = await sheetColumns(page.request, pid, sheetId);
  const mediaB = initialColumns.find((column) => column.name === 'media_b');
  expect(mediaB).toBeTruthy();

  const renameRefresh = await gateNextSheetList(page, pid);
  await clickHeaderMenu(page, initialColumns, 'media_b');
  await page.getByTestId('header-menu-action-media.transcribe').click();
  await renameRefresh.started;
  const source = page.getByTestId('field-source');
  await expect(source.locator('option:checked')).toContainText('media_b');
  await page.getByTestId('field-output-text').fill('dirty-survives-rename');

  mutateColumn(pid, sheetId, String(mediaB!.id), 'rename', 'media_b_renamed');
  renameRefresh.release();
  await expect(source.locator('option:checked')).toContainText('media_b_renamed');
  await expect(page.getByTestId('field-output-text')).toHaveValue('dirty-survives-rename');

  await page.getByTestId('action-drawer-close').click();
  await expect(page.getByTestId('action-drawer')).toHaveCount(0);
  const renamedColumns = await sheetColumns(page.request, pid, sheetId);
  const deleteRefresh = await gateNextSheetList(page, pid);
  await clickHeaderMenu(page, renamedColumns, 'media_b_renamed');
  await page.getByTestId('header-menu-action-media.transcribe').click();
  await deleteRefresh.started;
  await expect(source.locator('option:checked')).toContainText('media_b_renamed');

  mutateColumn(pid, sheetId, String(mediaB!.id), 'delete');
  deleteRefresh.release();
  await expect(page.getByTestId('generated-action-run')).toBeDisabled();
  await expect(page.getByTestId('generated-action-run')).toHaveAttribute('title',
    /media_b_renamed|source column.*(?:missing|deleted|no longer exists)/i,
  );
  await expect
    .poll(async () => (await source.locator('option:checked').textContent()) ?? '')
    .not.toContain('media_a');
});
