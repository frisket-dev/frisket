import { readFileSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';

// Exercise the actual styles in both import orders. Component ownership must
// survive lazy loading and Storybook without depending on the app's module graph.
const styles = (files: string[]) => files.map(file => readFileSync(path.resolve('src', file), 'utf8'));
const foundation = readFileSync(path.resolve('src/styles.css'), 'utf8');

test('import rail owns its desktop and narrow layout independently of the dialog', async ({ page }) => {
  const sheets = styles(['components/importWorkspace/ImportModeRail.css', 'components/importWorkspace/ImportWorkspaceDialog.css']);
  for (const width of [1440, 720]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const order of [sheets, [...sheets].reverse()]) {
      await page.setContent(`<style>${foundation}\n${order.join('\n')}</style>
        <div class="import-mode-list" style="width:600px">
          ${['One file', 'Multiple files', 'Split a PDF', 'Feed or source'].map(label => `<button class="import-mode"><span>${label}</span><small>Import documents</small></button>`).join('')}
        </div>`);
      const columns = await page.locator('.import-mode-list').evaluate(element => getComputedStyle(element).gridTemplateColumns.split(' ').length);
      expect(columns).toBe(width > 760 ? 1 : 3);
      await expect(page.locator('.import-mode').first()).toHaveCSS('padding', '9px 10px');
    }
  }
});

test('shared controls retain their styling when feature CSS loads first or last', async ({ page }) => {
  const sheets = styles([
    'components/Button.css', 'components/FormControls.css', 'components/PanelSelect.css',
    'components/MenuPop.css', 'components/ExportDialogs.css', 'workbench/DiscoverPanel.css',
    'components/ProjectAskDock.css', 'workspace/WorkspaceToolbar.css',
    'components/importWorkspace/ImportWorkspaceShared.css',
    'components/project-ask/AskResearch.css', 'components/importWorkspace/tableImport/TableImportFlow.css',
    'components/SourcesPanel.css', 'components/ActivityProgress.css', 'components/StatusBar.css',
    'components/HomeScreen.css', 'components/action-panel/OutputFieldsEditor.css',
  ]);
  const markup = `
    <button id="export" class="btn export-option-btn">Export</button>
    <div class="menu-pop discover-overflow-menu"><button id="discover" class="menu-item discover-overflow-item" data-active="true">Sources</button></div>
    <label id="destination" class="form-field import-destination-choice-field">Sheet name<input class="form-input"></label>
    <label id="research" class="form-field ask-research-field">Question<input class="form-input"></label>
    <label id="approval" class="form-field ask-research-approval-field">Limit<input class="form-input"></label>
    <label id="dedup" class="form-field table-import-dedup">Skip duplicates<input type="checkbox"></label>
    <span id="spinner" class="run-spinner activity-progress-spinner"></span>
    <span id="idle" class="run-label">idle</span>
    <div id="home-menu" class="menu-pop home-card-menu">Projects</div>
    <div id="ask-menu" class="menu-pop ask-thread-menu">Conversations</div>
    <div id="saved-menu" class="menu-pop saved-views-overflow-menu">Views</div>
    <button id="required" class="btn btn-icon output-field-required" data-button-variant="text" aria-pressed="true">*</button>
    <button id="optional" class="btn btn-icon output-field-required" data-button-variant="text" aria-pressed="false">*</button>
    <button id="focus" class="form-input select-trigger">Choose a column</button>`;
  const properties = ['display', 'flex-direction', 'gap', 'padding', 'font-size', 'font-weight', 'color', 'background-color', 'justify-content', 'border-radius', 'outline', 'outline-offset', 'width', 'height'];
  const samples = [];
  for (const order of [[foundation, ...sheets], [...sheets].reverse().concat(foundation)]) {
    await page.setContent(`<style>${order.join('\n')}</style>${markup}`);
    await page.locator('#focus').focus();
    samples.push(await page.locator('[id]').evaluateAll((elements, keys) => Object.fromEntries(elements.map(element => {
      const computed = getComputedStyle(element);
      return [element.id, Object.fromEntries(keys.map(key => [key, computed.getPropertyValue(key)]))];
    })), properties));
  }
  expect(samples[1]).toEqual(samples[0]);
  for (const id of ['required', 'optional']) {
    const toggle = page.locator(`#${id}`);
    await expect(toggle).toHaveCSS('border-width', '0px');
    await expect(toggle).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
    await toggle.hover();
    await expect(toggle).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
  }
  await expect(page.locator('#required')).toHaveCSS('color', 'rgb(179, 57, 47)');
  await expect(page.locator('#optional')).toHaveCSS('color', 'rgb(143, 136, 121)');
});
