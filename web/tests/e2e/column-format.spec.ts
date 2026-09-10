import { expect, test } from '@playwright/test';
import {
  clickCell,
  clickHeaderMenu,
  createProject,
  importCsv,
  openCellDrawer,
  runAndWait,
  sheetColumns,
  type WireColumn,
  uniqueName,
} from './helpers';

async function openColumnSettings(page: import('@playwright/test').Page, columns: WireColumn[], name: string) {
  const column = columns.find((c) => c.name === name);
  expect(column).toBeTruthy();
  await clickHeaderMenu(page, columns, name);
  await page.getByTestId('header-menu-column-settings').click();
  await expect(page.getByTestId('column-drawer')).toBeVisible();
}

test('column drawer retypes columns and edits display formats', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-column-format'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'note,bytes,ratio\n"**Bold** memo",2048,0.25\n"plain memo",512,0.5\n',
  );
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  let legacyColumnPatchCalled = false;
  const v1ColumnPatchPayloads: unknown[] = [];
  await page.route(`**/api/projects/${pid}/columns/*`, async (route) => {
    const request = route.request();
    if (request.method() === 'PATCH') {
      const body = request.postDataJSON() as Record<string, unknown>;
      if (
        Object.prototype.hasOwnProperty.call(body, 'type') ||
        Object.prototype.hasOwnProperty.call(body, 'format')
      ) {
        legacyColumnPatchCalled = true;
        await route.fulfill({ status: 599, body: 'legacy column PATCH blocked' });
        return;
      }
    }
    await route.continue();
  });
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (
      request.method() === 'POST' &&
      url.pathname === `/api/projects/${pid}/actions/v1/run`
    ) {
      const body = request.postDataJSON() as Record<string, unknown>;
      if (body.action_id === 'column.patch') v1ColumnPatchPayloads.push(body);
    }
  });

  let columns = await sheetColumns(page.request, pid, sheetId);
  await openColumnSettings(page, columns, 'note');
  const noteColumn = columns.find((c) => c.name === 'note');
  expect(noteColumn).toBeTruthy();
  await expect(page.getByTestId('column-save-button')).toBeDisabled();
  await expect(page.getByTestId('column-type-help')).toHaveCount(0);
  await expect(page.getByTestId('column-type-validation-hint')).toHaveCount(0);
  await expect(page.getByTestId('column-format-help')).toHaveCount(0);
  await expect(page.getByTestId('column-type-select')).toHaveClass(/row-height-select/);
  await expect(page.getByTestId('column-format-select')).toHaveClass(/row-height-select/);
  await expect(
    page.getByTestId('column-format-select').locator('option[value="filesize"]'),
  ).toHaveCount(0);
  await expect(
    page.getByTestId('column-format-select').locator('option[value="currency"]'),
  ).toHaveCount(0);
  const markdownPatchRequest = page.waitForRequest((request) => {
    if (request.method() !== 'POST') return false;
    if (new URL(request.url()).pathname !== `/api/projects/${pid}/actions/v1/run`) {
      return false;
    }
    const body = request.postDataJSON() as {
      action_id?: string;
      params?: { column_id?: number; format?: string | null };
    };
    return (
      body.action_id === 'column.patch' &&
      body.params?.column_id === noteColumn!.id &&
      body.params?.format === 'markdown'
    );
  });
  await expect(page.getByTestId('column-type-select')).toHaveValue('text');
  await page.getByTestId('column-format-select').selectOption('markdown');
  await page.getByTestId('column-save-button').click();
  const markdownPatchPayload = markdownPatchRequest.then((request) => request.postDataJSON() as {
    action_id?: string;
    scope?: { kind?: string };
    output_names?: Record<string, string>;
    idempotency_key?: string;
    params?: { column_id?: number; format?: string | null };
  });
  await expect(page.getByTestId('column-update-result')).toContainText('Column updated');
  await expect(markdownPatchRequest).resolves.toBeTruthy();
  await expect(markdownPatchPayload).resolves.toMatchObject({
    action_id: 'column.patch',
    scope: { kind: 'project' },
    output_names: {},
    params: {
      column_id: noteColumn!.id,
      format: 'markdown',
    },
  });
  await expect(markdownPatchPayload).resolves.toEqual(
    expect.objectContaining({ idempotency_key: expect.stringMatching(/^web-column\.patch:/) }),
  );

  columns = await sheetColumns(page.request, pid, sheetId);
  expect(columns.find((c) => c.name === 'note')?.format).toBe('markdown');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('column-drawer')).toBeHidden();
  await clickCell(page, columns, 'note', 0);
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('markdown-value')).toContainText('Bold');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('row-drawer')).toBeHidden();

  await openColumnSettings(page, columns, 'note');
  await page.getByTestId('column-type-select').selectOption('date');
  await expect(page.getByTestId('column-format-select')).toHaveValue('');
  await expect(
    page.getByTestId('column-format-select').locator('option[value="markdown"]'),
  ).toHaveCount(0);
  await expect(page.getByTestId('column-format-help')).toHaveCount(0);
  await expect(page.getByTestId('column-type-help')).toHaveCount(0);
  await page.getByTestId('column-save-button').click();
  await expect(page.getByTestId('column-update-error')).toContainText(
    'Existing values in this column cannot be saved as date',
  );
  await expect(page.getByTestId('column-update-error')).toContainText(
    'Edit the values or choose another type',
  );

  await page.getByTestId('column-type-select').selectOption('text');
  const clearPatchRequest = page.waitForRequest((request) => {
    if (request.method() !== 'POST') return false;
    if (new URL(request.url()).pathname !== `/api/projects/${pid}/actions/v1/run`) {
      return false;
    }
    const body = request.postDataJSON() as {
      action_id?: string;
      params?: { column_id?: number; format?: string | null };
    };
    return (
      body.action_id === 'column.patch' &&
      body.params?.column_id === noteColumn!.id &&
      body.params?.format === null
    );
  });
  await page.getByTestId('column-format-select').selectOption('');
  await page.getByTestId('column-save-button').click();
  await expect(page.getByTestId('column-update-result')).toContainText('Column updated');
  await expect(clearPatchRequest).resolves.toBeTruthy();
  columns = await sheetColumns(page.request, pid, sheetId);
  expect(columns.find((c) => c.name === 'note')?.format ?? null).toBeNull();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('column-drawer')).toBeHidden();

  await openColumnSettings(page, columns, 'bytes');
  await expect(page.getByTestId('column-update-error')).toHaveCount(0);
  await expect(page.getByTestId('column-update-result')).toHaveCount(0);
  const bytesColumn = columns.find((c) => c.name === 'bytes');
  expect(bytesColumn).toBeTruthy();
  await expect(
    page.getByTestId('column-format-select').locator('option[value="markdown"]'),
  ).toHaveCount(0);
  await expect(page.getByTestId('column-format-select').locator('option[value="filesize"]')).toHaveCount(1);
  await expect(page.getByTestId('column-format-help')).toHaveCount(0);
  const setTypeRequest = page.waitForRequest((request) => {
    if (request.method() !== 'POST') return false;
    if (new URL(request.url()).pathname !== `/api/projects/${pid}/actions/v1/run`) {
      return false;
    }
    const body = request.postDataJSON() as {
      action_id?: string;
      params?: { column_id?: number; type?: string };
    };
    return (
      body.action_id === 'column.set_type' &&
      body.params?.column_id === bytesColumn!.id &&
      body.params?.type === 'number'
    );
  });
  await page.getByTestId('column-type-select').selectOption('number');
  await page.getByTestId('column-save-button').click();
  const setTypePayload = setTypeRequest.then((request) => request.postDataJSON() as {
    action_id?: string;
    scope?: { kind?: string };
    output_names?: Record<string, string>;
    idempotency_key?: string;
    params?: { column_id?: number; type?: string };
  });
  await expect(page.getByTestId('column-update-result')).toContainText('Column updated');
  await expect(setTypeRequest).resolves.toBeTruthy();
  await expect(setTypePayload).resolves.toMatchObject({
    action_id: 'column.set_type',
    scope: { kind: 'project' },
    output_names: {},
    params: {
      column_id: bytesColumn!.id,
      type: 'number',
    },
  });
  await expect(setTypePayload).resolves.toEqual(
    expect.objectContaining({ idempotency_key: expect.stringMatching(/^web-column\.set_type:/) }),
  );
  columns = await sheetColumns(page.request, pid, sheetId);
  expect(columns.find((c) => c.name === 'bytes')?.type).toBe('number');

  const filesizePatchRequest = page.waitForRequest((request) => {
    if (request.method() !== 'POST') return false;
    if (new URL(request.url()).pathname !== `/api/projects/${pid}/actions/v1/run`) {
      return false;
    }
    const body = request.postDataJSON() as {
      action_id?: string;
      params?: { column_id?: number; format?: string | null };
    };
    return (
      body.action_id === 'column.patch' &&
      body.params?.column_id === bytesColumn!.id &&
      body.params?.format === 'filesize'
    );
  });
  await page.getByTestId('column-format-select').selectOption('filesize');
  await page.getByTestId('column-save-button').click();
  await expect(page.getByTestId('column-update-result')).toContainText('Column updated');
  await expect(filesizePatchRequest).resolves.toBeTruthy();
  columns = await sheetColumns(page.request, pid, sheetId);
  expect(columns.find((c) => c.name === 'bytes')?.format).toBe('filesize');
  expect(legacyColumnPatchCalled).toBe(false);
  expect(v1ColumnPatchPayloads).toHaveLength(3);
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('column-drawer')).toBeHidden();

  // 'bytes' is a plain non-AI number column (now in-place editable —
  // grid-in-place-edit-v1), so the floating icon, not Enter, opens the drawer.
  await openCellDrawer(page, columns, 'bytes', 0);
  await expect(page.getByTestId('row-drawer')).toContainText('2 KB');
});

test('AI-generated text columns can be saved as markdown from the column drawer', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-ai-column-markdown'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'note\n"quarterly numbers"\n',
  );
  await runAndWait(page.request, pid, {
    action_id: 'map.template',
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: { template: { text: '**{{note}}**' } },
    output_names: { rendered: 'summary' },
    idempotency_key: `e2e-ai-column-markdown-map.template:${pid}:${sheetId}`,
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  let columns = await sheetColumns(page.request, pid, sheetId);
  expect(columns.find((c) => c.name === 'summary')?.ai_generated).toBe(true);

  await openColumnSettings(page, columns, 'summary');
  await page.getByTestId('column-format-select').selectOption('markdown');
  await page.getByTestId('column-save-button').click();
  await expect(page.getByTestId('column-update-result')).toContainText('Column updated');

  columns = await sheetColumns(page.request, pid, sheetId);
  expect(columns.find((c) => c.name === 'summary')?.format).toBe('markdown');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('column-drawer')).toBeHidden();

  await clickCell(page, columns, 'summary', 0);
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('markdown-value').locator('strong')).toHaveText(
    'quarterly numbers',
  );
});
