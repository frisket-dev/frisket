import { expect, type Page } from '@playwright/test';
import { clickHeader, createProject, importCsv, openCellDrawer, openProject, sheetColumns } from '../e2e/helpers';

type Shot = (page: Page, family: string, name: string, source: string) => Promise<void>;

export async function captureInspectionScenes(page: Page, shot: Shot) {
  const pid = await createProject(page.request, 'Inspection inventory');
  const sid = await importCsv(page.request, pid, 'records.csv', 'name,notes\nAda,Council meeting\nMax,Planning hearing\n');
  const result = await page.request.post(`/api/projects/${pid}/actions/v1/run`, { data: {
    action_id: 'map.template', scope: { kind: 'sheet_rows', sheet_id: sid },
    params: { template: { text: '{{name}} — {{notes}}' } }, output_names: { rendered: 'label' },
    idempotency_key: 'inventory-inspection-template',
  } });
  expect(result.ok()).toBeTruthy();
  expect((await result.json()).status).toBe('completed');
  const columns = await sheetColumns(page.request, pid, sid);
  await openProject(page, pid, sid);
  const overflowTrigger = page.getByTestId('toolbar-overflow-button');
  await overflowTrigger.click();
  await page.getByTestId('open-saved-views-submenu').hover();
  await expect(page.getByTestId('saved-views-overflow-menu')).toBeVisible();
  await overflowTrigger.focus();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('saved-views-overflow-menu')).toHaveCount(0);
  await expect(page.getByTestId('toolbar-overflow-menu')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('toolbar-overflow-menu')).toHaveCount(0);
  await openCellDrawer(page, columns, 'label', 0);
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await shot(page, 'Inspection', 'row-and-cell-provenance', 'src/components/RowDrawer.tsx; cell provenance');
  await openProject(page, pid, sid);
  await clickHeader(page, columns, 'label');
  await expect(page.getByTestId('column-drawer')).toBeVisible();
  await shot(page, 'Inspection', 'column-history', 'src/components/ColumnDrawer.tsx');
  await openProject(page, pid, sid);
  await page.keyboard.press('Control+k');
  await expect(page.getByTestId('workbench-region-commandPalette')).toBeVisible();
  await page.getByTestId('command-palette-input').fill('extract');
  await expect(page.getByTestId('palette-section-actions')).toBeVisible();
  await shot(page, 'Inspection', 'command-search', 'src/workbench/WorkbenchCommandPalette.tsx');
}

export async function captureActionScenes(page: Page, shot: Shot) {
  const pid = await createProject(page.request, 'Actions inventory');
  const sid = await importCsv(page.request, pid, 'records.csv',
    'name,notes,amount\nAda,Council approved the housing contract,1200\nMax,School board met on Tuesday,50\n');
  await importCsv(page.request, pid, 'districts.csv', 'name,district\nAda,North\nMax,South\n');
  for (const id of ['map.template', 'map.classify', 'map.extract', 'map.ner', 'map.translate',
    'media.ocr', 'media.transcribe', 'derive.join', 'map.python', 'map.api_call',
    'resolve.combine', 'resolve.replace', 'resolve.substitute']) {
    await page.goto(`/p/${pid}/s/${sid}/action/${id}`);
    const form = page.locator('[data-testid="action-form"], [data-testid="generated-action-form"]');
    await expect(form).toBeVisible();
    await expect(page.getByText('Loading action…', { exact: true })).toHaveCount(0);
    await expect(form.getByText('Loading choices…', { exact: true })).toHaveCount(0);
    if (id === 'map.template') {
      const footer = await form.locator('.action-run-actions').boundingBox();
      const drawer = await page.locator('.action-drawer').boundingBox();
      expect(footer).not.toBeNull(); expect(drawer).not.toBeNull();
      expect(Math.abs(footer!.y + footer!.height - drawer!.y - drawer!.height)).toBeLessThan(3);
      expect(footer!.width).toBeGreaterThan(drawer!.width - 5);
      expect(await form.locator('.action-run-actions').evaluate(el => getComputedStyle(el).alignItems)).toBe('stretch');
    }
    await shot(page, 'Actions', id.replaceAll('.', '-'), 'src/components/action-panel/; src/actions/');
    if (id === 'map.template') {
      const changeAction = page.getByTestId('action-title-menu-button');
      await changeAction.focus();
      await page.keyboard.press('Enter');
      await expect(page.getByTestId('action-title-menu')).toBeVisible();
      await page.keyboard.press('Tab');
      await expect(page.getByTestId('action-title-menu').getByRole('menuitem').first()).toBeFocused();
      await page.keyboard.press('Escape');
      await expect(page.getByTestId('action-title-menu')).toHaveCount(0);
      await expect(changeAction).toBeFocused();
      await page.getByTestId('action-help-button').click();
      await expect(page.getByTestId('action-help-popover')).toBeVisible();
      await shot(page, 'Actions', 'action-help', 'src/components/action-panel/ActionHelpPopover.tsx');
      await page.keyboard.press('Escape');
      await expect(page.getByTestId('action-help-popover')).toHaveCount(0);
      await expect(form).toBeVisible();
    }
    if (id === 'resolve.replace') {
      await page.setViewportSize({ width: 1024, height: 900 });
      await shot(page, 'Actions', 'replace-narrow', 'src/components/resolve/ReplaceForm.tsx');
      await page.setViewportSize({ width: 1440, height: 1000 });
    }
    if (id === 'map.extract') {
      // A real open picker supplements the closed form, without configuring or calling a provider.
      await form.getByTestId('engine-selector').getByRole('button', { expanded: false }).click();
      await expect(page.getByTestId('engine-selector-dialog')).toBeVisible();
      await expect(page.getByTestId('model-route-control')).toBeVisible();
      await shot(page, 'Actions', 'extract-model-picker', 'src/components/engine-selector/EngineSelector.tsx');
      await page.keyboard.press('Escape');
      await page.setViewportSize({ width: 1024, height: 900 });
      await shot(page, 'Actions', 'extract-narrow', 'src/components/action-panel/ExtractParamsBody.tsx');
      await page.setViewportSize({ width: 1440, height: 1000 });
    }
  }
}

export async function captureAskAndComparisonScenes(page: Page, shot: Shot) {
  const pid = await createProject(page.request, 'Ask and comparison inventory');
  const sid = await importCsv(page.request, pid, 'stories.csv', 'story\nCouncil approved the contract\nSchool board met on Tuesday\n');
  await openProject(page, pid, sid);
  await page.getByTestId('chrome-ask-toggle').click();
  await expect(page.getByTestId('ask-dock')).toBeVisible();
  await shot(page, 'Ask', 'ask-no-provider', 'src/components/ProjectAskDock.tsx');
  await page.getByTestId('ask-options-trigger').click();
  await shot(page, 'Ask', 'ask-options', 'src/components/ProjectAskDock.tsx');
  await page.keyboard.press('Escape');
  await page.getByTestId('ask-dock').getByRole('button', { name: 'Add sources', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Choose project sources' })).toBeVisible();
  await shot(page, 'Ask', 'ask-source-picker', 'src/components/ProjectSourcePicker.tsx');
  await page.keyboard.press('Escape');

  for (const [command, tab] of [['ocr-compare', 'ocr-compare'], ['transcribe-compare', 'transcribe-compare'],
    ['translate-compare', 'translate-compare'], ['topic-compare', 'topic-compare']]) {
    await openProject(page, pid, sid);
    // Search the actual ribbon tabs; comparison commands are not action drawers.
    const button = page.getByTestId(`ribbon-command-${command}`);
    const tabs = page.getByTestId('act-ribbon').getByRole('tab');
    for (let i = 0; i < await tabs.count() && !await button.isVisible(); i++) await tabs.nth(i).click();
    await button.click();
    await expect(page.getByTestId(`${tab}-tab`)).toBeVisible();
    if (command === 'ocr-compare') {
      const source = page.getByTestId('ocr-compare-source-toggle');
      await expect(source).toBeVisible();
      expect(await source.evaluate(el => getComputedStyle(el).borderTopWidth)).toBe('1px');
      const prior = await source.getAttribute('aria-pressed');
      await source.click();
      await expect(source).toHaveAttribute('aria-pressed', prior === 'true' ? 'false' : 'true');
      await source.click();
    }
    await shot(page, 'Comparison', `${tab}-empty`, 'src/workbench/MediaCompareShell.tsx; *CompareTab.tsx');
  }
}
