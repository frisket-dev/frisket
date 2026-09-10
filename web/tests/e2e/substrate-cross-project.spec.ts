// Proves createWorkspaceStores(projectId) actually isolates a project's gridViewStore
// state on a CLIENT-SIDE project switch, not merely that the next project's
// own persisted defaults happen to look clean. A plain page.goto() would
// reload the page and trivially reset everything regardless of whether the
// substrate isolates anything — this spec drives the project switcher (the
// ⌄ dropdown, `navigate({kind:'project',...})`) so <Workspace
// key={project.id}> actually remounts and WorkspaceStoresProvider's
// projectId-keyed factory actually reconstructs, in one page session.
//
// The row selection this spec drives is selectionStore's field, not the
// legacy reducer's — same assertions, new owning store, no behavior change.
// A SECOND domain — a detailStore row-drawer open in A — pins isolation for
// two distinct stores, not just one.
//
// Pins TODAY'S actual behavior (investigated below), not an ideal: switching
// back to project A does NOT restore A's filter/selection/drawer — there is
// no cross-mount persistence for any of them. That was already true pre-
// migration (the <Workspace key> remount discards the plain useReducer ui
// state the same way); this spec proves gridViewStore/selectionStore/
// detailStore now hold up the SAME contract.

import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openFriendlyFilterSidebar,
  openCellDrawer,
  openProject,
  selectRow,
  sheetColumns,
  uniqueName,
} from './helpers';

test('cross-project isolation: a grid filter, a row selection, and an open row drawer in project A do not leak into project B, and do not survive switching back to A', async ({
  page,
}) => {
  const pidA = await createProject(page.request, uniqueName('substrate-xproj-a'));
  const sheetA = await importCsv(
    page.request,
    pidA,
    'rows.csv',
    'city,status\nAlbany,open\nBuffalo,closed\n',
  );
  const pidB = await createProject(page.request, uniqueName('substrate-xproj-b'));
  const sheetB = await importCsv(
    page.request,
    pidB,
    'rows.csv',
    'city,status\nSyracuse,open\nRochester,closed\n',
  );

  // --- Set state in project A: a grid filter (gridViewStore), a row
  // selection (selectionStore), and an open row drawer (detailStore). ---
  await openProject(page, pidA, sheetA);
  const columnsA = await sheetColumns(page.request, pidA, sheetA);
  await openFriendlyFilterSidebar(page, columnsA, 'status');
  await page.getByTestId('facet-check-status-open').check();
  await expect(page.getByTestId('active-grid-filter')).toContainText('status');
  await selectRow(page, 0);
  await expect(page.getByTestId('delete-rows-button')).toBeVisible();
  // 'city' is a plain non-AI text column (now in-place editable —
  // grid-in-place-edit-v1), so the floating icon, not Enter, opens the drawer.
  await openCellDrawer(page, columnsA, 'city', 0);
  await expect(page.getByTestId('row-drawer')).toBeVisible();

  // --- Client-side switch to project B via the project switcher (NOT
  // page.goto — that would be a full reload and prove nothing about
  // in-memory isolation). ---
  await page.getByTestId('switch-project').click();
  await page.getByTestId(`menu-project-${pidB}`).click();
  await expect(page).toHaveURL(new RegExp(`/p/${pidB}`));
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  void sheetB;

  // B shows neither A's filter, selection, nor open row drawer.
  await expect(page.getByTestId('active-grid-filter')).toHaveCount(0);
  await expect(page.getByTestId('delete-rows-button')).toHaveCount(0);
  await expect(page.getByTestId('row-drawer')).toHaveCount(0);

  // --- Switch BACK to project A. Pin today's actual behavior: filter,
  // selection, and drawer do NOT come back — none of gridViewStore/
  // selectionStore/detailStore persists across a project remount. ---
  await page.getByTestId('switch-project').click();
  await page.getByTestId(`menu-project-${pidA}`).click();
  await expect(page).toHaveURL(new RegExp(`/p/${pidA}`));
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await expect(page.getByTestId('active-grid-filter')).toHaveCount(0);
  await expect(page.getByTestId('delete-rows-button')).toHaveCount(0);
  await expect(page.getByTestId('row-drawer')).toHaveCount(0);
});
