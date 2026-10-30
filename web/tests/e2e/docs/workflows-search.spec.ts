import { expect, test } from '@playwright/test';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { createProject, importCsv, openProject, textPdf } from '../helpers';
import { captureCheckpoint, publishCaptureBundle, type CaptureRecord } from './capture';

test.use({ deviceScaleFactor: 2, viewport: { width: 1600, height: 1000 } });
test.skip(!process.env.FRISKET_DOCS_PROOF_DIR, 'Run through the isolated documentation capture harness');
test.beforeAll(() => {
  const root = path.resolve('..');
  const git = (...args: string[]) => execFileSync('git', args, { cwd: root, encoding: 'utf8' }).trim();
  expect(process.env.FRISKET_DOCS_REVISION).toBe(git('rev-parse', 'HEAD'));
  const changes = git('status', '--porcelain', '--untracked-files=no').split('\n').filter(Boolean);
  const permittedOverlay = process.env.FRISKET_DOCS_PACKAGE_OVERLAY === '1' ? ['M web/package.json'] : [];
  expect(changes.filter(change => !permittedOverlay.includes(change))).toEqual([]);
});

test('search, Ask, document, export and skill workflows on a disposable project', async ({ page }) => {
  test.setTimeout(180_000);
  const pid = await createProject(page.request, 'Council records documentation');
  const sheetId = await importCsv(page.request, pid, 'council-records.csv', [
    'title,notes',
    'Transit hearing,Council discussed the transit budget at the hearing.',
    'Library meeting,Members considered the library schedule.',
  ].join('\n') + '\n');
  const imported = await page.request.post(`/api/projects/${pid}/import/files?sheet_name=Council%20documents`, {
    multipart: { files: {
      name: 'transit-hearing.pdf', mimeType: 'application/pdf',
      buffer: textPdf([
        ['COUNCIL HEARING', 'The council discussed the transit budget.'],
        ['MEETING NOTES', 'The council requested a revised budget.'],
      ]),
    } },
  });
  expect(imported.ok()).toBeTruthy();
  const sheets = await (await page.request.get(`/api/projects/${pid}/sheets`)).json() as Array<{ id: number; name: string }>;
  const documentSheet = sheets.find(sheet => sheet.name === 'Council documents');
  expect(documentSheet).toBeDefined();
  await openProject(page, pid, sheetId);

  const output = path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'search-workflows');
  await publishCaptureBundle(output, {
    appRevision: process.env.FRISKET_DOCS_REVISION!,
    sample: { id: 'search-workflows-fixture', revision: 1 },
  }, async stage => {
    const captures: CaptureRecord[] = [];
    const capture = async (id: string, root: ReturnType<typeof page.locator>) => {
      await expect(root).toBeVisible();
      captures.push(await captureCheckpoint(page, { id, root, annotations: [] }, stage));
    };

    const chrome = page.getByTestId('chrome-bar');
    await expect(chrome.getByTestId('chrome-command-pill')).toBeVisible();
    await capture('workflow-search-entry', chrome);
    await chrome.getByTestId('chrome-command-pill').click();
    const palette = page.getByTestId('workbench-region-commandPalette');
    await expect(palette).toBeVisible();
    await palette.getByTestId('search-mode-select').selectOption('lexical');
    await palette.getByTestId('command-palette-input').fill('transit');
    const results = palette.getByTestId('search-results');
    await expect(results).toBeVisible({ timeout: 30_000 });
    await expect(results.getByTestId('search-hit').first()).toContainText(/transit/i);
    await capture('workflow-search-results', palette);
    await palette.getByRole('button', { name: 'Close command palette' }).click();

    await expect(chrome.getByTestId('chrome-ask-toggle')).toBeVisible();
    await capture('workflow-ask-entry', chrome);
    await chrome.getByTestId('chrome-ask-toggle').click();
    const dock = page.getByTestId('ask-dock');
    await expect(dock).toBeVisible();
    await dock.getByRole('textbox', { name: 'Question' }).fill('What did the council discuss about transit?');
    await dock.getByRole('button', { name: 'Add sources' }).click();
    const sources = page.getByRole('dialog', { name: 'Choose project sources' });
    await expect(sources).toBeVisible();
    await expect(sources.getByRole('checkbox', { name: 'Whole project' })).not.toBeChecked();
    await expect(sources.getByRole('checkbox', { name: 'Council documents' })).toBeVisible();
    await capture('workflow-ask-sources', sources);
    await sources.getByRole('button', { name: 'Use sources' }).click();
    await dock.getByRole('button', { name: 'Ask options' }).click();
    const options = page.getByRole('dialog', { name: 'Ask settings' });
    await expect(options.getByRole('checkbox', { name: 'Run actions' })).toBeVisible();
    await expect(options.getByText('Suggest actions')).toBeVisible();
    await capture('workflow-ask-settings', options);
    await dock.getByRole('button', { name: 'Ask options' }).click();
    await dock.getByRole('button', { name: 'Collapse Ask' }).click();

    await page.getByTestId(`workbench-mainView-tab-${documentSheet!.id}`).click();
    await expect(page.getByTestId('view-switch-document')).toBeVisible();
    await capture('workflow-document-entry', page.getByTestId('workbench-region-mainView'));
    await page.getByTestId('view-switch-document').click();
    const document = page.getByTestId('document-view');
    await expect(document).toBeVisible();
    await expect(page.getByTestId('document-pdf')).toHaveAttribute('data-page-count', '2', { timeout: 30_000 });
    await expect(page.getByTestId('document-list-item')).toContainText('transit-hearing.pdf');
    await capture('workflow-document-view', document);

    await page.getByTestId('view-switch-grid').click();
    await page.getByTestId(`workbench-mainView-tab-${sheetId}`).click();
    await expect(page.getByTestId('grid')).toBeVisible();
    await page.getByTestId('switch-project').click();
    const projectMenu = page.getByTestId('project-menu');
    await projectMenu.getByTestId('export-menu-open').click();
    await expect(projectMenu.getByTestId('export-target-csv')).toHaveText('CSV or Excel');
    await capture('workflow-export-entry', projectMenu);
    await projectMenu.getByTestId('export-target-csv').click();
    const exportDialog = page.getByTestId('export-data-modal');
    await expect(exportDialog).toBeVisible();
    await expect(exportDialog.getByTestId('export-format-csv')).toBeChecked();
    await expect(exportDialog.getByText('Entire selected sheets')).toBeVisible();
    await capture('workflow-export-data', exportDialog);
    const downloadPromise = page.waitForEvent('download');
    await exportDialog.getByTestId('export-dataset-download').click();
    const download = await downloadPromise;
    const downloadPath = await download.path();
    expect(downloadPath).toBeTruthy();
    expect((await readFile(downloadPath!, 'utf8'))).toContain('Council discussed the transit budget');

    await page.goto('/settings/personal/skills');
    const skills = page.getByTestId('skills-settings');
    await expect(skills).toBeVisible();
    await skills.getByRole('button', { name: 'New skill' }).click();
    const editor = skills.getByTestId('skills-editor');
    await editor.fill('---\nname: check-council-records\ndescription: Review council records and cite the source.\n---\n\n# Check council records\n\nInspect the selected records and cite the supporting source.\n');
    await skills.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(skills.getByRole('button', { name: /check-council-records/ })).toBeVisible();
    await expect(editor).toHaveValue(/name: check-council-records/);
    await capture('workflow-skills-settings', skills);

    expect(captures.map(item => item.id)).toEqual([
      'workflow-search-entry', 'workflow-search-results', 'workflow-ask-entry',
      'workflow-ask-sources', 'workflow-ask-settings', 'workflow-document-entry',
      'workflow-document-view', 'workflow-export-entry', 'workflow-export-data',
      'workflow-skills-settings',
    ]);
    return captures;
  });
});
