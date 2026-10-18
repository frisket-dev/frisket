import { mkdir, writeFile } from 'node:fs/promises';
import { expect, test, type Locator, type Page } from '@playwright/test';
import {
  commitTableImport,
  createProject,
  importCsv,
  listSheets,
  openImportWorkspace,
  reviewTableColumns,
  sheetData,
  uniqueName,
} from './helpers';

function minimalPdf(text: string): Buffer {
  const content = `BT /F1 24 Tf 72 700 Td (${text}) Tj ET`;
  const objects = [
    '1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n',
    '2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n',
    '3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n',
    `4 0 obj<</Length ${Buffer.byteLength(content)}>>stream\n${content}\nendstream\nendobj\n`,
    '5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n',
  ];
  let out = '%PDF-1.4\n';
  const offsets: number[] = [];
  for (const object of objects) {
    offsets.push(Buffer.byteLength(out));
    out += object;
  }
  const xref = Buffer.byteLength(out);
  out += 'xref\n0 6\n0000000000 65535 f \n';
  for (const offset of offsets) {
    out += `${offset.toString().padStart(10, '0')} 00000 n \n`;
  }
  out += `trailer<</Size 6/Root 1 0 R>>\nstartxref\n${xref}\n%%EOF`;
  return Buffer.from(out, 'utf-8');
}

/** A one-cell OOXML workbook. The planner receives actual workbook bytes, not
 * a filename-only stand-in, while the browser test remains self-contained. */
function minimalXlsx(value: string): Buffer {
  const escapeXml = (text: string) => text.replace(/[<>&"']/g, (character) => ({
    '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&apos;',
  })[character]!);
  const entries = [
    ['[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'],
    ['_rels/.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'],
    ['xl/workbook.xml', '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'],
    ['xl/_rels/workbook.xml.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'],
    ['xl/worksheets/sheet1.xml', `<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>${escapeXml(value)}</t></is></c></row></sheetData></worksheet>`],
  ] as const;
  const crc32 = (data: Buffer) => {
    let crc = 0xffffffff;
    for (const byte of data) {
      crc ^= byte;
      for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
    return (crc ^ 0xffffffff) >>> 0;
  };
  const parts: Buffer[] = [];
  const central: Buffer[] = [];
  let offset = 0;
  for (const [name, content] of entries) {
    const filename = Buffer.from(name);
    const data = Buffer.from(content);
    const checksum = crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt32LE(checksum, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(filename.length, 26);
    const directory = Buffer.alloc(46);
    directory.writeUInt32LE(0x02014b50, 0);
    directory.writeUInt16LE(20, 4);
    directory.writeUInt16LE(20, 6);
    directory.writeUInt32LE(checksum, 16);
    directory.writeUInt32LE(data.length, 20);
    directory.writeUInt32LE(data.length, 24);
    directory.writeUInt16LE(filename.length, 28);
    directory.writeUInt32LE(offset, 42);
    parts.push(local, filename, data);
    central.push(directory, filename);
    offset += local.length + filename.length + data.length;
  }
  const directoryBytes = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(directoryBytes.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...parts, directoryBytes, end]);
}

async function dropFiles(
  page: Page,
  files: Array<{ name: string; mimeType: string; buffer: Buffer }>,
) {
  const dataTransfer = await page.evaluateHandle((browserFiles) => {
    const transfer = new DataTransfer();
    for (const browserFile of browserFiles) {
      const bytes = Uint8Array.from(
        atob(browserFile.base64),
        (character) => character.charCodeAt(0),
      );
      transfer.items.add(new File([bytes], browserFile.name, { type: browserFile.mimeType }));
    }
    return transfer;
  }, files.map((file) => ({
    name: file.name,
    mimeType: file.mimeType,
    base64: file.buffer.toString('base64'),
  })));

  const dropzone = page.getByTestId('import-dropzone');
  await dropzone.dispatchEvent('dragover', { dataTransfer });
  await dropzone.dispatchEvent('drop', { dataTransfer });
}

function multipartFieldValues(body: string, field: string): string[] {
  return [...body.matchAll(new RegExp(`name="${field}"\\r\\n\\r\\n([^\\r]+)`, 'g'))]
    .map((match) => match[1]);
}

function multipartFilenames(body: string): string[] {
  return [...body.matchAll(/name="files"; filename="([^"]+)"/g)].map((match) => match[1]);
}

function bulkPreparation(plan: Record<string, unknown>): string {
  return JSON.stringify({ kind: 'bulk', plan });
}

async function openFilesImport(page: Page) {
  await openImportWorkspace(page);
  const tableSource = page.getByRole('region', { name: 'Add data', exact: true });
  const filesMode = page.getByTestId('import-mode-files');
  await expect(tableSource.or(filesMode)).toBeVisible();
  if (await tableSource.isVisible()) {
    await page.getByRole('button', { name: 'Multiple files', exact: true }).click();
  }
  await expect(filesMode).toHaveAttribute('aria-selected', 'true');
}

async function chooseZip(page: Page, picker: Locator, name: string) {
  const chooser = page.waitForEvent('filechooser');
  await picker.click();
  await (await chooser).setFiles({
    name,
    mimeType: 'application/zip',
    buffer: Buffer.from('not parsed by the route mock'),
  });
}

const EMPTY_DROPZONE_BULK_CASES = [
  {
    label: 'single EML',
    files: [{
      name: 'message.eml',
      mimeType: 'message/rfc822',
      buffer: Buffer.from('From: source@example.test\nTo: desk@example.test\nSubject: Tip\n\nBody\n'),
    }],
    expandArchive: false,
  },
  {
    label: 'single MBOX',
    files: [{
      name: 'mailbox.mbox',
      mimeType: 'application/mbox',
      buffer: Buffer.from('From source@example.test Thu Jan  1 00:00:00 2026\nSubject: Tip\n\nBody\n'),
    }],
    expandArchive: false,
  },
  {
    label: 'single ZIP',
    files: [{
      name: 'mail.zip',
      mimeType: 'application/zip',
      buffer: Buffer.from('route mock does not parse the archive'),
    }],
    expandArchive: true,
  },
  {
    label: 'single extensionless mail file',
    files: [{
      name: 'message',
      mimeType: 'message/rfc822',
      buffer: Buffer.from('From: source@example.test\nTo: desk@example.test\nSubject: Tip\n\nBody\n'),
    }],
    expandArchive: false,
  },
  {
    label: 'two CSV files',
    files: [
      { name: 'inbox.csv', mimeType: 'text/csv', buffer: Buffer.from('subject\nTip\n') },
      { name: 'sent.csv', mimeType: 'text/csv', buffer: Buffer.from('subject\nReply\n') },
    ],
    expandArchive: false,
  },
] as const;

for (const scenario of EMPTY_DROPZONE_BULK_CASES) {
  test(`empty-project dropzone sends ${scenario.label} through bulk planning`, async ({ page }) => {
    const pid = await createProject(page.request, uniqueName('e2e-empty-drop-bulk'));
    let planBody = '';
    let legacyFilesCount = 0;
    await page.route(`**/api/projects/${pid}/import/files/prepare`, async (route) => {
      planBody = route.request().postData() ?? '';
      await route.fulfill({
        contentType: 'application/json',
        body: bulkPreparation({
          plan_id: 'empty-drop-plan',
          questions: [],
          proposed_outputs: [],
          message: 'No importable files found.',
        }),
      });
    });
    await page.route(`**/api/projects/${pid}/import/files`, async (route) => {
      legacyFilesCount += 1;
      await route.fulfill({ status: 500, body: 'bulk-capable drops must not use legacy file import' });
    });

    await page.goto(`/p/${pid}`);
    await expect(page.getByTestId('import-dropzone')).toBeVisible({ timeout: 15_000 });
    await dropFiles(page, [...scenario.files]);

    await expect.poll(() => ({ planned: planBody !== '', legacyFilesCount })).toEqual({
      planned: true,
      legacyFilesCount: 0,
    });
    expect(multipartFilenames(planBody)).toEqual(scenario.files.map((file) => file.name));
    expect(multipartFieldValues(planBody, 'logical_paths')).toEqual(
      scenario.files.map((file) => file.name),
    );
    expect(multipartFieldValues(planBody, 'expand_archive')).toEqual([
      String(scenario.expandArchive),
    ]);
  });
}

test('empty-project dropzone keeps a single ordinary file on legacy file import', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-empty-drop-legacy'));
  let preparationCount = 0;
  let legacyFilesCount = 0;
  await page.route(`**/api/projects/${pid}/import/files/prepare`, async (route) => {
    preparationCount += 1;
    await route.fulfill({ status: 500, body: 'ordinary single files must not use bulk planning' });
  });
  await page.route(`**/api/projects/${pid}/import/files`, async (route) => {
    legacyFilesCount += 1;
    await route.fulfill({ status: 500, body: 'expected legacy route sentinel' });
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('import-dropzone')).toBeVisible({ timeout: 15_000 });
  await dropFiles(page, [{
    name: 'notes.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('ordinary notes\n'),
  }]);

  await expect.poll(() => ({ preparationCount, legacyFilesCount })).toEqual({
    preparationCount: 0,
    legacyFilesCount: 1,
  });
});

test('toolbar import opens a modal with table stages and other intake paths', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-import-workspace'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  await page.goto(`/p/${pid}`);

  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openImportWorkspace(page);

  const workspace = page.getByTestId('import-workspace');
  await expect(workspace).toBeVisible();
  const dialog = page.getByTestId('import-workspace-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveJSProperty('open', true);
  await expect(page.getByTestId('import-menu')).toHaveCount(0);
  for (const stage of ['add', 'rows', 'columns', 'import']) {
    await expect(page.getByTestId(`table-import-stage-${stage}`)).toBeVisible();
  }
  await expect(page.getByTestId('table-import-stage-add')).toHaveAttribute('aria-current', 'step');
  await page.getByRole('button', { name: 'Paste rows', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Paste rows', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Choose a file', exact: true }).click();
  await page.getByRole('button', { name: 'Multiple files', exact: true }).click();
  await expect(page.getByTestId('table-import-stage-add')).toHaveCount(0);

  for (const mode of ['csv', 'files', 'feed']) {
    await expect(page.getByTestId(`import-mode-${mode}`)).toBeVisible();
  }
  await expect(page.getByTestId('import-mode-paste')).toHaveCount(0);
  await expect(page.getByTestId('import-source-urls')).toBeVisible();
  await expect(page.getByTestId('import-mode-xlsx')).toHaveCount(0);
  await expect(page.getByTestId('import-mode-pdf')).toHaveCount(0);
});

test('CSV folder selection plans a bulk import, keeps paths aligned, and executes separate sheets', async ({ page }, testInfo) => {
  const pid = await createProject(page.request, uniqueName('e2e-bulk-csv'));
  const seedSheetId = await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  let planBody = '';
  let executeBody: unknown = null;
  await page.route(`**/api/projects/${pid}/import/files/prepare`, async (route) => {
    planBody = route.request().postData() ?? '';
    await route.fulfill({
      contentType: 'application/json',
      body: bulkPreparation({
        plan_id: 'matching-csv-plan',
        questions: [{
          id: 'matching-csv',
          kind: 'csv_combine',
          default: 'combine',
          logical_paths: ['mail/inbox.csv', 'mail/sent.csv'],
        }],
        proposed_outputs: [{
          id: 'messages',
          kind: 'csv_group',
          sheet_name: 'Messages',
          logical_paths: ['mail/inbox.csv', 'mail/sent.csv'],
        }],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/import/bulk/matching-csv-plan/execute`, async (route) => {
    executeBody = route.request().postDataJSON();
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        created: [{
          id: 'messages', kind: 'sheet', sheet_id: 999, sheet_name: 'Messages',
          logical_paths: ['mail/inbox.csv', 'mail/sent.csv'],
        }],
        failed: [],
        // Deliberately differs from created[0] so this proves the UI follows
        // the server-designated landing sheet after refreshing sheets.
        first_sheet_id: seedSheetId,
        message: 'Imported 2 files.',
      }),
    });
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openFilesImport(page);
  const workspace = page.getByTestId('import-workspace');
  const folderPicker = workspace.getByRole('button', { name: /folder/i });
  await expect(folderPicker).toBeVisible();
  const folderChooser = page.waitForEvent('filechooser');
  await folderPicker.click();
  const selectedFolder = await folderChooser;
  const folderInput = await selectedFolder.element();
  expect(await folderInput.getAttribute('webkitdirectory')).not.toBeNull();
  expect(await selectedFolder.isMultiple()).toBeTruthy();
  const mailDirectory = testInfo.outputPath('mail');
  await mkdir(mailDirectory, { recursive: true });
  await Promise.all([
    writeFile(`${mailDirectory}/inbox.csv`, 'subject,body\nHello,World\n'),
    writeFile(`${mailDirectory}/sent.csv`, 'subject,body\nReply,Thanks\n'),
  ]);
  await selectedFolder.setFiles(mailDirectory);

  await expect.poll(() => planBody).not.toBe('');
  const filenames = multipartFilenames(planBody);
  const logicalPaths = multipartFieldValues(planBody, 'logical_paths');
  expect(filenames).toEqual(logicalPaths);
  expect(logicalPaths.toSorted()).toEqual(['mail/inbox.csv', 'mail/sent.csv']);
  expect(multipartFieldValues(planBody, 'expand_archive')).toEqual(['false']);

  await expect(workspace.getByLabel(/combine/i)).toBeVisible();
  const separate = workspace.getByLabel(/separate/i);
  await expect(separate).toBeVisible();
  await separate.check();
  await workspace.getByRole('button', { name: /^Execute import$/i }).click();

  await expect.poll(() => executeBody).toEqual({ decisions: { 'matching-csv': 'separate' } });
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${seedSheetId}$`));
});

test('a sole ZIP plans with archive expansion', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-bulk-zip'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  let planBody = '';
  await page.route(`**/api/projects/${pid}/import/files/prepare`, async (route) => {
    planBody = route.request().postData() ?? '';
    await route.fulfill({
      contentType: 'application/json',
      body: bulkPreparation({ plan_id: 'zip-plan', questions: [], proposed_outputs: [], message: 'Ready.' }),
    });
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openFilesImport(page);
  const workspace = page.getByTestId('import-workspace');
  const zipPicker = workspace.getByRole('button', { name: /zip/i });
  await expect(zipPicker).toBeVisible();
  await chooseZip(page, zipPicker, 'mail-export.zip');

  await expect.poll(() => planBody).not.toBe('');
  expect(multipartFilenames(planBody)).toEqual(['mail-export.zip']);
  expect(multipartFieldValues(planBody, 'logical_paths')).toEqual(['mail-export.zip']);
  expect(multipartFieldValues(planBody, 'expand_archive')).toEqual(['true']);
});

test('a question-bearing CSV plan waits for a choice and an execute click', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-bulk-csv-question'));
  const seedSheetId = await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  let executeBody: unknown = null;
  await page.route(`**/api/projects/${pid}/import/files/prepare`, async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: bulkPreparation({
        plan_id: 'csv-question-plan',
        questions: [{
          id: 'matching-csv',
          kind: 'csv_combine',
          default: 'combine',
          logical_paths: ['inbox.csv', 'sent.csv'],
        }],
        proposed_outputs: [{
          id: 'messages',
          kind: 'csv_group',
          sheet_name: 'Messages',
          logical_paths: ['inbox.csv', 'sent.csv'],
        }],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/import/bulk/csv-question-plan/execute`, async (route) => {
    executeBody = route.request().postDataJSON();
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        created: [{
          id: 'messages', kind: 'sheet', sheet_id: seedSheetId, sheet_name: 'Messages',
          logical_paths: ['inbox.csv', 'sent.csv'],
        }],
        failed: [],
        first_sheet_id: seedSheetId,
        message: 'Imported 2 files.',
      }),
    });
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openFilesImport(page);
  const workspace = page.getByTestId('import-workspace');
  await workspace.getByTestId('import-file-input').setInputFiles([
    { name: 'inbox.csv', mimeType: 'text/csv', buffer: Buffer.from('subject\nHello\n') },
    { name: 'sent.csv', mimeType: 'text/csv', buffer: Buffer.from('subject\nReply\n') },
  ]);

  await expect(workspace.getByLabel(/combine/i)).toBeVisible();
  const separate = workspace.getByLabel(/separate/i);
  await expect(separate).toBeVisible();
  const execute = workspace.getByRole('button', { name: /^Execute import$/i });
  await expect(execute).toBeEnabled();
  expect(executeBody).toBeNull();

  await separate.check();
  await execute.click();
  await expect.poll(() => executeBody).toEqual({ decisions: { 'matching-csv': 'separate' } });
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${seedSheetId}$`));
});

test('closing a planned bulk import clears it before the workspace reopens', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-bulk-close-reset'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  let planCount = 0;
  await page.route(`**/api/projects/${pid}/import/files/prepare`, async (route) => {
    planCount += 1;
    await route.fulfill({
      contentType: 'application/json',
      body: bulkPreparation({
        plan_id: 'close-reset-plan',
        questions: [{
          id: 'matching-csv',
          kind: 'csv_combine',
          default: 'combine',
          logical_paths: ['inbox.csv', 'sent.csv'],
        }],
        proposed_outputs: [{
          id: 'messages',
          kind: 'csv_group',
          sheet_name: 'Messages',
          logical_paths: ['inbox.csv', 'sent.csv'],
        }],
      }),
    });
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openFilesImport(page);
  const workspace = page.getByTestId('import-workspace');
  await workspace.getByTestId('import-file-input').setInputFiles([
    { name: 'inbox.csv', mimeType: 'text/csv', buffer: Buffer.from('subject\nHello\n') },
    { name: 'sent.csv', mimeType: 'text/csv', buffer: Buffer.from('subject\nReply\n') },
  ]);
  await expect(workspace.getByLabel(/combine/i)).toBeVisible();
  expect(planCount).toBe(1);

  await workspace.getByTestId('import-workspace-close').click();
  await expect(workspace).toHaveCount(0);
  await openFilesImport(page);
  await expect(workspace.getByRole('heading', { name: 'Proposed outputs' })).toHaveCount(0);
  await expect(workspace.getByLabel(/combine/i)).toHaveCount(0);
  expect(planCount).toBe(1);
});

test('a question-free bulk plan executes exactly once without showing an execute control', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-bulk-auto-execute'));
  const seedSheetId = await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  let planCount = 0;
  let executeCount = 0;
  let releaseExecute = () => {};
  const executeGate = new Promise<void>((resolve) => {
    releaseExecute = resolve;
  });
  await page.route(`**/api/projects/${pid}/import/files/prepare`, async (route) => {
    planCount += 1;
    await route.fulfill({
      contentType: 'application/json',
      body: bulkPreparation({
        plan_id: 'auto-execute-plan',
        questions: [],
        proposed_outputs: [{
          id: 'emails', kind: 'email', sheet_name: 'Emails', logical_paths: ['mail/message.eml'],
        }],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/import/bulk/auto-execute-plan/execute`, async (route) => {
    executeCount += 1;
    await executeGate;
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        created: [{
          id: 'emails', kind: 'sheet', sheet_id: seedSheetId, sheet_name: 'Emails',
          logical_paths: ['mail/message.eml'],
        }],
        failed: [],
        first_sheet_id: seedSheetId,
        message: 'Imported 1 sheet.',
      }),
    });
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openFilesImport(page);
  const workspace = page.getByTestId('import-workspace');
  await chooseZip(page, workspace.getByRole('button', { name: /zip/i }), 'mail-export.zip');

  await expect.poll(() => planCount).toBe(1);
  await expect.poll(() => executeCount).toBe(1);
  await expect(
    workspace.getByRole('button', { name: /^(Execute import|Importing…)$/i }),
  ).toHaveCount(0);

  releaseExecute();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${seedSheetId}$`));
  expect(executeCount).toBe(1);
});

test('Files accepts two XLSX workbooks, bulk-plans them once, and opens the designated worksheet', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-bulk-multi-xlsx'));
  const activeSheetId = await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  let planBody = '';
  let planCount = 0;
  let executeCount = 0;
  let legacyFilesCount = 0;
  let legacyXlsxCount = 0;
  let releaseExecute = () => {};
  const executeGate = new Promise<void>((resolve) => { releaseExecute = resolve; });
  await page.route(`**/api/projects/${pid}/import/files/prepare`, async (route) => {
    planCount += 1;
    planBody = route.request().postData() ?? '';
    await route.fulfill({
      contentType: 'application/json',
      body: bulkPreparation({
        plan_id: 'two-xlsx-plan',
        questions: [],
        proposed_outputs: [{
          id: 'workbooks', kind: 'xlsx', sheet_name: 'Imported workbooks',
          logical_paths: ['north.xlsx', 'south.xlsx'],
        }],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/import/bulk/two-xlsx-plan/execute`, async (route) => {
    executeCount += 1;
    expect(route.request().postDataJSON()).toEqual({ decisions: {} });
    await executeGate;
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        created: [{
          id: 'workbooks', kind: 'sheet', sheet_id: activeSheetId, sheet_name: 'Imported workbooks',
          logical_paths: ['north.xlsx', 'south.xlsx'],
        }],
        failed: [], first_sheet_id: activeSheetId, message: 'Imported 2 workbooks.',
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/import/files`, async (route) => {
    legacyFilesCount += 1;
    await route.fulfill({ status: 500, body: 'multi-workbook selection must use bulk planning' });
  });
  await page.route(`**/api/projects/${pid}/import/xlsx`, async (route) => {
    legacyXlsxCount += 1;
    await route.fulfill({ status: 500, body: 'multi-workbook selection must not use direct XLSX import' });
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openFilesImport(page);
  const workspace = page.getByTestId('import-workspace');
  const picker = workspace.getByTestId('import-file-input');
  await expect(picker).toHaveAttribute('accept', /(?:^|,)\.xlsx(?:,|$)/);
  await picker.setInputFiles([
    { name: 'north.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: minimalXlsx('North') },
    { name: 'south.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: minimalXlsx('South') },
  ]);

  await expect.poll(() => planCount).toBe(1);
  expect(multipartFilenames(planBody)).toEqual(['north.xlsx', 'south.xlsx']);
  expect(multipartFieldValues(planBody, 'logical_paths')).toEqual(['north.xlsx', 'south.xlsx']);
  expect(multipartFieldValues(planBody, 'expand_archive')).toEqual(['false']);
  await expect.poll(() => executeCount).toBe(1);
  expect(legacyFilesCount).toBe(0);
  expect(legacyXlsxCount).toBe(0);
  await expect(workspace.getByLabel(/combine into one sheet/i)).toHaveCount(0);
  await expect(workspace.getByRole('button', { name: /^(Execute import|Importing…)$/i })).toHaveCount(0);

  releaseExecute();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${activeSheetId}$`));
  expect(planCount).toBe(1);
  expect(executeCount).toBe(1);
});

test('two generic Files selections with one PDF stay on the normal file import', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-bulk-generic-pair'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  let preparationCount = 0;
  await page.route(`**/api/projects/${pid}/import/files/prepare`, async (route) => {
    preparationCount += 1;
    await route.fulfill({ status: 500, body: 'one-PDF generic selections must not use staged preparation' });
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openFilesImport(page);
  const workspace = page.getByTestId('import-workspace');
  await workspace.getByTestId('import-file-input').setInputFiles([
    { name: 'note.txt', mimeType: 'text/plain', buffer: Buffer.from('one note') },
    { name: 'docket.pdf', mimeType: 'application/pdf', buffer: minimalPdf('generic pair') },
  ]);

  await expect.poll(
    async () => (await listSheets(page.request, pid)).some((sheet) => sheet.name === 'files'),
    { timeout: 15_000 },
  ).toBe(true);
  const filesSheet = (await listSheets(page.request, pid)).find((sheet) => sheet.name === 'files');
  expect(filesSheet).toBeTruthy();
  await expect.poll(
    async () => (await sheetData(page.request, pid, filesSheet!.id)).rows.length,
    { timeout: 15_000 },
  ).toBe(2);
  const data = await sheetData(page.request, pid, filesSheet!.id);
  const filenameCol = data.columns.find((column) => column.name === 'filename');
  const mediaCol = data.columns.find((column) => column.name === 'media');
  const sizeCol = data.columns.find((column) => column.name === 'size');
  expect(filenameCol).toBeTruthy();
  expect(mediaCol).toBeTruthy();
  expect(sizeCol).toBeTruthy();
  const rowsByFilename = new Map(data.rows.map((row) => [
    row.cells[String(filenameCol!.id)],
    row,
  ]));
  expect([...rowsByFilename.keys()].toSorted()).toEqual(['docket.pdf', 'note.txt']);
  expect(rowsByFilename.get('docket.pdf')?.cells[String(mediaCol!.id)]).toMatchObject({
    filename: 'docket.pdf',
    mime: 'application/pdf',
  });
  expect(rowsByFilename.get('note.txt')?.cells[String(sizeCol!.id)]).toBe(8);
  expect(preparationCount).toBe(0);
});

test('a directly selected extensionless file is sent to the bulk planner', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-bulk-extensionless'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  let planBody = '';
  let legacyImportCount = 0;
  await page.route(`**/api/projects/${pid}/import/files/prepare`, async (route) => {
    planBody = route.request().postData() ?? '';
    await route.fulfill({
      contentType: 'application/json',
      body: bulkPreparation({
        plan_id: 'extensionless-plan',
        questions: [],
        proposed_outputs: [],
        message: 'No importable files found.',
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/import/files`, async (route) => {
    legacyImportCount += 1;
    await route.fulfill({ status: 500, body: 'extensionless files must use bulk planning' });
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openFilesImport(page);
  const workspace = page.getByTestId('import-workspace');
  await workspace.getByTestId('import-file-input').setInputFiles({
    name: 'evidence',
    mimeType: 'application/octet-stream',
    buffer: Buffer.from('extensionless evidence\n'),
  });

  await expect.poll(() => ({ planned: planBody !== '', legacyImportCount })).toEqual({
    planned: true,
    legacyImportCount: 0,
  });
  expect(multipartFilenames(planBody)).toEqual(['evidence']);
  expect(multipartFieldValues(planBody, 'logical_paths')).toEqual(['evidence']);
  expect(multipartFieldValues(planBody, 'expand_archive')).toEqual(['false']);
  await expect(workspace.getByRole('status')).toContainText('No importable files found.');
});

test('bulk email warnings remain inert text alongside a successful import', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-bulk-email-warning'));
  const seedSheetId = await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  const warning = 'mail/bad.eml: <img src="not-a-preview"> has no recognizable headers';
  await page.route(`**/api/projects/${pid}/import/files/prepare`, async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: bulkPreparation({
        plan_id: 'email-warning-plan',
        questions: [],
        proposed_outputs: [{
          id: 'emails', kind: 'email', sheet_name: 'Emails', logical_paths: ['mail/good.eml', 'mail/bad.eml'],
        }],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/import/bulk/email-warning-plan/execute`, async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        created: [{
          id: 'emails', kind: 'sheet', sheet_id: 999, sheet_name: 'Emails',
          logical_paths: ['mail/good.eml', 'mail/bad.eml'],
        }],
        failed: [],
        first_sheet_id: seedSheetId,
        message: 'Imported 1 sheet.',
        warnings: [warning],
      }),
    });
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openFilesImport(page);
  const workspace = page.getByTestId('import-workspace');
  const zipPicker = workspace.getByRole('button', { name: /zip/i });
  await chooseZip(page, zipPicker, 'mail-export.zip');

  await expect(workspace).toContainText(warning);
  await expect(workspace.locator('img[src="not-a-preview"]')).toHaveCount(0);
  await expect(workspace.getByRole('button', { name: /^Execute import$/i })).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${seedSheetId}$`));
});

test('bulk email warnings remain visible alongside a partial import failure', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-bulk-email-partial-warning'));
  const seedSheetId = await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  const warning = 'mail/bad.eml: <img src="not-a-preview"> has no recognizable headers';
  const partialMessage = 'Imported 1 sheet; 1 output failed.';
  await page.route(`**/api/projects/${pid}/import/files/prepare`, async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: bulkPreparation({
        plan_id: 'email-partial-warning-plan',
        questions: [],
        proposed_outputs: [{
          id: 'emails', kind: 'email', sheet_name: 'Emails', logical_paths: ['mail/good.eml', 'mail/bad.eml'],
        }],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/import/bulk/email-partial-warning-plan/execute`, async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        created: [{
          id: 'emails', kind: 'sheet', sheet_id: seedSheetId, sheet_name: 'Emails',
          logical_paths: ['mail/good.eml', 'mail/bad.eml'],
        }],
        failed: [{ id: 'broken', logical_path: 'broken.csv', reason: 'CSV row width mismatch' }],
        first_sheet_id: seedSheetId,
        message: partialMessage,
        warnings: [warning],
      }),
    });
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openFilesImport(page);
  const workspace = page.getByTestId('import-workspace');
  const zipPicker = workspace.getByRole('button', { name: /zip/i });
  await chooseZip(page, zipPicker, 'mail-export.zip');

  await expect(workspace).toContainText(partialMessage);
  await expect(workspace).toContainText(warning);
  await expect(workspace.locator('img[src="not-a-preview"]')).toHaveCount(0);
  await expect(workspace.getByRole('button', { name: /^Execute import$/i })).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${seedSheetId}$`));
});

test('a bulk plan with no proposed outputs reports that status and cannot execute', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-bulk-empty'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  let planCount = 0;
  let executeCount = 0;
  await page.route(`**/api/projects/${pid}/import/files/prepare`, async (route) => {
    planCount += 1;
    await route.fulfill({
      contentType: 'application/json',
      body: bulkPreparation({
        plan_id: 'empty-plan', questions: [], proposed_outputs: [], message: 'No importable files found.',
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/import/bulk/empty-plan/execute`, async (route) => {
    executeCount += 1;
    await route.fulfill({ status: 500, body: 'an empty plan must never execute' });
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openFilesImport(page);
  const workspace = page.getByTestId('import-workspace');
  const zipPicker = workspace.getByRole('button', { name: /zip/i });
  await expect(zipPicker).toBeVisible();
  await chooseZip(page, zipPicker, 'metadata-only.zip');

  await expect.poll(() => planCount).toBe(1);
  await expect(workspace.getByRole('status')).toContainText('No importable files found.');
  await expect(workspace.getByRole('button', { name: /^Execute import$/i })).toHaveCount(0);
  expect(executeCount).toBe(0);
});

test('paste import stages the original source and preserves inferred numeric values', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-paste-import'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  let legacyCsvRequests = 0;
  await page.route(`**/api/projects/${pid}/import/csv`, async route => {
    legacyCsvRequests += 1;
    await route.fulfill({ status: 500, json: { error: 'paste must use the table draft flow' } });
  });
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openImportWorkspace(page);
  await page.getByRole('button', { name: 'Paste rows', exact: true }).click();
  const raw = 'name\tscore\nAda Lovelace\t7\nGrace Hopper\t9\n';
  await page.getByRole('textbox', { name: 'Paste rows', exact: true }).fill(raw);
  const staged = page.waitForRequest(request =>
    new URL(request.url()).pathname === `/api/projects/${pid}/import/table-imports/paste`
    && request.method() === 'POST');
  await page.getByRole('button', { name: 'Check rows →', exact: true }).click();
  expect((await staged).postData()).toContain(raw);
  const rows = page.getByRole('region', { name: 'Check rows', exact: true });
  await expect(rows.getByRole('table')).toContainText('Grace Hopper');
  await expect(rows.getByLabel('Character encoding')).toHaveCount(0);
  await reviewTableColumns(page);
  await expect(page.getByLabel('Sheet name', { exact: true })).toHaveValue('Pasted rows');
  const sheetId = await commitTableImport(page, pid);
  await expect.poll(async () => (await sheetData(page.request, pid, sheetId)).total).toBe(2);
  const data = await sheetData(page.request, pid, sheetId);
  const nameCol = data.columns.find(column => column.name === 'name')!;
  const scoreCol = data.columns.find(column => column.name === 'score')!;
  expect(scoreCol.type).toBe('integer');
  expect(data.rows[0].cells[String(nameCol.id)]).toBe('Ada Lovelace');
  expect(data.rows[1].cells[String(nameCol.id)]).toBe('Grace Hopper');
  expect(data.rows[1].cells[String(scoreCol.id)]).toBe(9);
  expect(legacyCsvRequests).toBe(0);
});

test('pasted rows review column choices and destination before writing', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-import-draft-flow'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  const beforeSheets = await listSheets(page.request, pid);
  await openImportWorkspace(page);
  await page.getByRole('button', { name: 'Paste rows', exact: true }).click();
  const pasteText = page.getByRole('textbox', { name: 'Paste rows', exact: true });
  const panelBox = await page.locator('.import-workspace-panel').boundingBox();
  const textBox = await pasteText.boundingBox();
  expect(panelBox).toBeTruthy();
  expect(textBox).toBeTruthy();
  expect(textBox!.width).toBeGreaterThan(600);
  expect(textBox!.x).toBeGreaterThanOrEqual(panelBox!.x);
  expect(textBox!.x + textBox!.width).toBeLessThanOrEqual(panelBox!.x + panelBox!.width);
  await pasteText.fill('name\tscore\nAda Lovelace\t7\nGrace Hopper\t9\n');
  const checkRows = page.getByRole('button', { name: 'Check rows →', exact: true });
  await expect(checkRows).toHaveClass(/btn-primary/);
  await checkRows.click();
  await expect(page.getByTestId('table-import-stage-rows')).toHaveAttribute('aria-current', 'step');
  await expect(page.getByRole('region', { name: 'Check rows', exact: true }).getByRole('table')).toContainText('Ada Lovelace');
  await page.getByRole('button', { name: 'Columns →', exact: true }).click();
  await expect(page.getByTestId('table-import-stage-columns')).toHaveAttribute('aria-current', 'step');
  await expect(page.getByLabel('Type for score', { exact: true })).toHaveValue('integer');
  await page.getByLabel('Type for name', { exact: true }).selectOption('omit');
  await page.getByLabel('Type for score', { exact: true }).selectOption('text');
  await page.getByRole('button', { name: 'Continue →', exact: true }).click();
  await expect(page.getByTestId('table-import-stage-import')).toHaveAttribute('aria-current', 'step');
  await page.getByLabel('Sheet name', { exact: true }).fill('Reviewed people');
  expect((await listSheets(page.request, pid)).map(sheet => sheet.id)).toEqual(beforeSheets.map(sheet => sheet.id));
  const sheetId = await commitTableImport(page, pid);
  await expect.poll(async () => (await sheetData(page.request, pid, sheetId)).total).toBe(2);
  const data = await sheetData(page.request, pid, sheetId);
  expect(data.columns.map(column => column.name)).toEqual(['score']);
  expect(data.columns[0].type).toBe('text');
  expect(data.rows[1].cells[String(data.columns[0].id)]).toBe('9');
  expect((await listSheets(page.request, pid)).find(sheet => sheet.id === sheetId)?.name).toBe('Reviewed people');
});

test('URL import records invalid URL errors', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-invalid-url-import'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  await page.goto(`/p/${pid}`);

  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openFilesImport(page);
  await page.getByTestId('import-source-urls').click();
  await page.getByTestId('import-url-list').fill('not-a-url');
  await page.getByTestId('import-url-submit').click();

  await expect
    .poll(async () => (await listSheets(page.request, pid)).some((s) => s.name === 'downloads'), {
      timeout: 15_000,
    })
    .toBeTruthy();
  const downloadsSheet = (await listSheets(page.request, pid)).find((s) => s.name === 'downloads');
  expect(downloadsSheet).toBeTruthy();
  const downloads = await sheetData(page.request, pid, downloadsSheet!.id);
  const urlCol = downloads.columns.find((c) => c.name === 'url');
  const errorCol = downloads.columns.find((c) => c.name === 'error');
  expect(urlCol).toBeTruthy();
  expect(errorCol).toBeTruthy();
  expect(downloads.rows[0].cells[String(urlCol!.id)]).toBe('not-a-url');
  expect(downloads.rows[0].cells[String(errorCol!.id)]).toContain('URL must start');
});
