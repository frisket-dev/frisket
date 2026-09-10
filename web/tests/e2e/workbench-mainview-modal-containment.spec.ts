import { expect, test } from '@playwright/test';
import { listSheets, openProject, openToolbarOverflow, projectIdByName } from './helpers';

let pid: string;

test.beforeEach(async ({ page }) => {
  pid = await projectIdByName(page.request, 'Local stories');
  const sheets = await listSheets(page.request, pid);
  await openProject(page, pid, sheets[0].id);
});

test('mainView and modalOrPeek expose serializable containment metadata', async ({ page }) => {
  const mainView = page.getByTestId('workbench-region-mainView');
  await expect(mainView).toHaveAttribute(
    'data-layout-state-schema',
    'frisket.workbench.mainview_layout.v1',
  );
  await expect(mainView).toHaveAttribute('data-active-contribution-id', 'frisket.core.view.grid');

  const grid = mainView.getByTestId('workbench-contribution-frisket-core-view-grid');
  await expect(grid).toBeVisible();
  await expect(grid).toHaveAttribute('data-host', 'mainView');
  await expect(grid).toHaveAttribute('data-slot', 'work.primary');

  const modalHost = page.getByTestId('workbench-region-modalOrPeek');
  await expect(modalHost).toHaveAttribute('data-modal-stack-host', 'true');
  const modalLayout = page.getByTestId('workbench-resolved-layout-region-modalOrPeek');
  await expect(modalLayout).toHaveAttribute('data-renderer', 'ResolvedWorkbenchLayoutRendererV1');
  await expect(modalLayout).toHaveAttribute('data-contribution-ids', /frisket\.core\.panel\.provenance/);

  await openToolbarOverflow(page);
  await page.getByTestId('open-provenance-manifest').click();
  const provenance = page.getByTestId('workbench-contribution-frisket-core-panel-provenance');
  await expect(provenance).toBeVisible();
  await expect(provenance).toHaveAttribute('data-host', 'modalOrPeek');
  await expect(provenance).toHaveAttribute('data-mode', 'peek');
  await expect(provenance).toHaveAttribute('data-slot', 'interruption');
});
