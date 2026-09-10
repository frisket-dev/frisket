import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';

test('mainView tabs are visible and prototype map move controls stay out of daily UI', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('workbench-structured-workspace'));
  const firstSheetId = await importCsv(
    page.request,
    pid,
    'briefing.csv',
    'snippet\n"First table row"\n',
  );
  const secondSheetId = await importCsv(
    page.request,
    pid,
    'leads.csv',
    'snippet\n"Second table row"\n',
  );

  await openProject(page, pid, firstSheetId);

  const mainView = page.getByTestId('workbench-region-mainView');
  const tabs = mainView.getByTestId('workbench-mainView-tabs');
  await expect(tabs).toHaveAttribute('data-host', 'mainView');
  await expect(tabs).toHaveAttribute('data-mode', 'tab');

  const briefingTab = tabs.getByTestId(`workbench-mainView-tab-${firstSheetId}`);
  const leadsTab = tabs.getByTestId(`workbench-mainView-tab-${secondSheetId}`);
  await expect(briefingTab).toHaveAttribute('data-contribution-id', 'frisket.core.view.grid');
  await expect(briefingTab).toHaveAttribute('aria-selected', 'true');
  await expect(leadsTab).toHaveAttribute('data-host', 'mainView');
  await leadsTab.click();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${secondSheetId}$`));
  await expect(leadsTab).toHaveAttribute('aria-selected', 'true');
  await expect(mainView.getByTestId('workbench-contribution-frisket-core-view-grid')).toBeVisible();
  await expect(page.getByTestId('sheet-stats')).toHaveText('1 rows · 1 columns');

  await expect(mainView.getByTestId('workbench-structured-move-source-frisket-geo-view-map')).toHaveCount(0);
  await expect(page.getByTestId('workbench-mainView-command-target')).toHaveCount(0);
  await expect(mainView).not.toContainText('Map target: mainView');
  await expect(mainView.getByTestId('grid')).toBeVisible();
});
