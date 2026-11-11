import fs from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { expect, test } from '@playwright/test';
import {
  createProject,
  editCells,
  importCsv,
  listSheets,
  openFriendlyFilterSidebar,
  openAdvancedSortPanel,
  openProject,
  sheetColumns,
  sheetData,
  uniqueName,
} from './helpers';

test('project menu exports the current sheet as CSV and the work log as Markdown', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('dataset-export'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'export rows.csv',
    'name,note\nAda,"hello, world"\nGrace,plain\n',
  );
  await openProject(page, pid, sheetId);

  await page.getByTestId('switch-project').click();
  await expect(page.getByTestId('project-menu')).toBeVisible();

  const csvDownloadPromise = page.waitForEvent('download');
  await page.getByTestId('export-menu-open').click();
  await page.getByTestId('export-target-csv').click();
  await expect(page.getByTestId('export-data-modal')).toBeVisible();
  await expect(page.getByTestId('export-include-attachments')).not.toBeChecked();
  await page.getByTestId('export-dataset-download').click();
  const csvDownload = await csvDownloadPromise;
  expect(csvDownload.suggestedFilename()).toBe('export-rows.csv');
  const csvPath = await csvDownload.path();
  expect(csvPath).toBeTruthy();
  const csvBytes = await fs.readFile(csvPath!);
  expect([...csvBytes.subarray(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);
  expect(csvBytes.toString('utf8')).toContain('Ada,"hello, world"');

  await page.getByTestId('switch-project').click();
  await expect(page.getByTestId('project-menu')).toBeVisible();

  const logDownloadPromise = page.waitForEvent('download');
  await page.getByTestId('export-work-log').click();
  const logDownload = await logDownloadPromise;
  expect(logDownload.suggestedFilename()).toMatch(/-work-log\.md$/);
  const logPath = await logDownload.path();
  expect(logPath).toBeTruthy();
  const logText = await fs.readFile(logPath!, 'utf8');
  expect(logText).toContain('# Work log:');
  expect(logText).toContain('## Operation History');
  expect(logText).toContain('| 1 | import.rows | import.csv | applied |');
  expect(logText).toContain('## Action Receipts');
  expect(logText).toContain('| import.csv | completed | 1 |');
  expect(logText).not.toContain('Recipe');
});

test('Export data packages an MP3 with its complete transcript and timed segments', async ({ page }, testInfo) => {
  const pid = await createProject(page.request, uniqueName('dataset-audio-export'));
  const audio = await fs.readFile(new URL(
    '../../../src/frisket/sample_content/audio/ann-arbor-policy-committee-2022-04-19-excerpt.mp3',
    import.meta.url,
  ));
  const imported = await page.request.post(`/api/projects/${pid}/import/files?sheet_name=Recordings`, {
    multipart: {
      files: { name: 'council-recording.mp3', mimeType: 'audio/mpeg', buffer: audio },
    },
  });
  expect(imported.ok()).toBeTruthy();
  const sheets = await listSheets(page.request, pid);
  const sheetId = sheets.find((sheet) => sheet.name === 'Recordings')!.id;
  for (const [name, type] of [['transcript', 'text'], ['segments', 'json']]) {
    const added = await page.request.post(`/api/projects/${pid}/actions/v1/run`, {
      data: {
        action_id: 'column.add',
        scope: { kind: 'project' },
        params: { sheet_id: sheetId, name, type },
        output_names: {},
        idempotency_key: `e2e-export-column:${pid}:${name}`,
      },
    });
    expect(added.ok()).toBeTruthy();
    expect((await added.json()).status).toBe('completed');
  }
  const transcript = 'Chair: "Welcome, everyone."\nWe will discuss the council budget.\nThank you — gracias.';
  const segments = [
    { start: 0, end: 2.75, text: 'Chair: "Welcome, everyone."' },
    { start: 2.75, end: 7.5, text: 'We will discuss the council budget.\nThank you — gracias.' },
  ];
  const data = await sheetData(page.request, pid, sheetId);
  await editCells(page.request, pid, [
    {
      rowId: data.rows[0].id,
      columnId: data.columns.find((column) => column.name === 'transcript')!.id,
      value: transcript,
    },
    {
      rowId: data.rows[0].id,
      columnId: data.columns.find((column) => column.name === 'segments')!.id,
      value: segments,
    },
  ]);
  await openProject(page, pid, sheetId);
  await page.getByTestId('switch-project').click();
  await page.getByTestId('export-menu-open').click();
  await page.getByTestId('export-target-csv').click();
  await page.getByTestId('export-include-attachments').check();
  await page.getByTestId('export-format-xlsx').check();
  await expect(page.getByTestId('export-include-attachments')).toBeChecked();
  await expect(page.getByTestId('export-dataset-download')).toHaveAttribute('href', /format=xlsx&include_attachments=true/);
  await page.getByTestId('export-format-csv').check();
  await expect(page.getByTestId('export-dataset-download')).toHaveText('Export ZIP');
  await testInfo.attach('export-with-attachments', {
    body: await page.getByTestId('export-data-modal').screenshot(),
    contentType: 'image/png',
  });

  const downloadPromise = page.waitForEvent('download');
  await page.getByTestId('export-dataset-download').click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/\.zip$/);
  const archivePath = await download.path();
  expect(archivePath).toBeTruthy();
  const inspection = JSON.parse(execFileSync(process.env.FRISKET_E2E_PYTHON ?? 'python3', ['-c', `
import csv, hashlib, io, json, sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as archive:
    names = archive.namelist()
    tables = [name for name in names if name.endswith('.csv')]
    attachments = {name: hashlib.sha256(archive.read(name)).hexdigest()
                   for name in names if name.startswith('attachments/') and not name.endswith('/')}
    rows = list(csv.DictReader(io.StringIO(archive.read(tables[0]).decode('utf-8-sig'))))
    print(json.dumps({'tables': tables, 'attachments': attachments, 'rows': rows}))
`, archivePath!], { encoding: 'utf8' }));
  expect(inspection.tables).toHaveLength(1);
  expect(inspection.rows).toHaveLength(1);
  expect(inspection.rows[0].transcript).toBe(transcript);
  expect(JSON.parse(inspection.rows[0].segments)).toEqual(segments);
  const attachmentPath = inspection.rows[0]['media.path'];
  expect(attachmentPath).toMatch(/^attachments\/[^/]+\/council-recording\.mp3$/);
  expect(Object.keys(inspection.attachments)).toEqual([attachmentPath]);
  expect(inspection.attachments[attachmentPath]).toBe(createHash('sha256').update(audio).digest('hex'));
});

test('project menu exports either the entire sheet or the active grid view as CSV', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('dataset-view-export'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'view rows.csv',
    [
      'title,status,published',
      'A start,todo,2026-01-01',
      'B done,done,2026-02-01',
      'C done,done,2026-03-01',
    ].join('\n') + '\n',
  );
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  const columns = await sheetColumns(page.request, pid, sheetId);
  await openFriendlyFilterSidebar(page, columns, 'status');
  const filterResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/sheets/${sheetId}/data` &&
      url.searchParams.has('filter')
    );
  });
  await page.getByTestId('facet-check-status-done').check();
  await filterResponse;

  await openAdvancedSortPanel(page, columns, 'title');
  await page.getByTestId('grid-sort-column').selectOption('published');
  await page.getByTestId('grid-sort-direction').selectOption('desc');
  const sortResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/sheets/${sheetId}/data` &&
      url.searchParams.has('filter') &&
      url.searchParams.has('sort')
    );
  });
  await page.getByTestId('apply-grid-sort').click();
  await sortResponse;

  await page.getByTestId('switch-project').click();
  await expect(page.getByTestId('project-menu')).toBeVisible();
  const fullDownloadPromise = page.waitForEvent('download');
  await page.getByTestId('export-menu-open').click();
  await page.getByTestId('export-target-csv').click();
  await expect(page.getByTestId('export-data-modal')).toBeVisible();
  await page.getByTestId('export-dataset-download').click();
  const fullDownload = await fullDownloadPromise;
  const fullPath = await fullDownload.path();
  expect(fullPath).toBeTruthy();
  const fullText = await fs.readFile(fullPath!, 'utf8');
  expect(fullText).toContain('A start,todo,2026-01-01');
  expect(fullText.indexOf('B done,done,2026-02-01')).toBeLessThan(
    fullText.indexOf('C done,done,2026-03-01'),
  );

  await page.getByTestId('switch-project').click();
  await expect(page.getByTestId('project-menu')).toBeVisible();
  const viewDownloadPromise = page.waitForEvent('download');
  await page.getByTestId('export-menu-open').click();
  await page.getByTestId('export-target-csv').click();
  await expect(page.getByTestId('export-data-modal')).toBeVisible();
  await page.getByTestId('export-scope-current-view').check();
  await page.getByTestId('export-dataset-download').click();
  const viewDownload = await viewDownloadPromise;
  const viewPath = await viewDownload.path();
  expect(viewPath).toBeTruthy();
  const viewText = await fs.readFile(viewPath!, 'utf8');
  expect(viewText).not.toContain('A start,todo,2026-01-01');
  expect(viewText.indexOf('C done,done,2026-03-01')).toBeLessThan(
    viewText.indexOf('B done,done,2026-02-01'),
  );
});

test('export picker downloads selected sheets as Excel tabs or a CSV ZIP', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('dataset-multi-export'));
  const casesId = await importCsv(
    page.request,
    pid,
    'Cases 2026.csv',
    'person,status\nJosé,open\n李雷,closed\n',
  );
  const meetingsId = await importCsv(
    page.request,
    pid,
    'Meeting notes.csv',
    'speaker,quote\nZoë,“smart quotes”\n',
  );
  await openProject(page, pid, casesId);

  const openExport = async () => {
    await page.getByTestId('switch-project').click();
    await page.getByTestId('export-menu-open').click();
    await page.getByTestId('export-target-csv').click();
    await expect(page.getByTestId('export-data-modal')).toBeVisible();
  };

  await openExport();
  await expect(page.getByTestId(`export-sheet-${casesId}`)).toBeChecked();
  await expect(page.getByTestId(`export-sheet-${meetingsId}`)).not.toBeChecked();
  await page.getByTestId('export-format-xlsx').check();
  await page.getByTestId('export-select-all-sheets').click();
  await expect(page.getByTestId('export-scope-current-view')).toBeDisabled();
  await expect(page.getByTestId('export-dataset-download')).toContainText(
    'Export 2 sheets to Excel',
  );
  const excelDownloadPromise = page.waitForEvent('download');
  await page.getByTestId('export-dataset-download').click();
  const excelDownload = await excelDownloadPromise;
  expect(excelDownload.suggestedFilename()).toMatch(/\.xlsx$/);
  const excelPath = await excelDownload.path();
  expect(excelPath).toBeTruthy();
  expect((await fs.readFile(excelPath!)).subarray(0, 2).toString()).toBe('PK');

  await openExport();
  await page.getByTestId('export-select-all-sheets').click();
  await expect(page.getByTestId('export-dataset-download')).toContainText(
    'Export 2 CSVs as ZIP',
  );
  const zipDownloadPromise = page.waitForEvent('download');
  await page.getByTestId('export-dataset-download').click();
  const zipDownload = await zipDownloadPromise;
  expect(zipDownload.suggestedFilename()).toMatch(/-csv\.zip$/);
  const zipPath = await zipDownload.path();
  expect(zipPath).toBeTruthy();
  expect((await fs.readFile(zipPath!)).subarray(0, 2).toString()).toBe('PK');
});
