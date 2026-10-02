import { expect, test } from '@playwright/test';
import {
  addRow,
  createProject,
  openProject,
  sheetColumns,
  sheetData,
  textPdf,
  uniqueName,
} from './helpers';

test('real document API composes cursor paging, selection, and title search', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('document-compose'));
  const imported = await page.request.post(`/api/projects/${pid}/import/files?sheet_name=documents`, {
    multipart: {
      files: {
        name: 'document-000.pdf',
        mimeType: 'application/pdf',
        buffer: textPdf([['Document zero']]),
      },
    },
  });
  expect(imported.ok()).toBeTruthy();
  const sheets = await (await page.request.get(`/api/projects/${pid}/sheets`)).json();
  const sheet = sheets.find((candidate: { name: string }) => candidate.name === 'documents');
  const columns = await sheetColumns(page.request, pid, sheet.id);
  const media = columns.find((column) => column.type === 'file')!;
  const filename = columns.find((column) => column.name === 'filename')!;
  const first = (await sheetData(page.request, pid, sheet.id, 0, 1)).rows[0];
  const blob = first.cells[String(media.id)];

  for (let index = 1; index <= 104; index += 1) {
    const title = index === 104 ? 'needle-final.pdf' : `document-${String(index).padStart(3, '0')}.pdf`;
    await addRow(page.request, pid, sheet.id, {
      [media.name]: { ...blob, filename: title },
      [filename.name]: title,
    });
  }

  await openProject(page, pid, sheet.id);
  await page.getByTestId('view-switch-document').click();
  await expect(page.getByText('100 loaded')).toBeVisible();

  const body = page.getByTestId('document-list-body');
  await body.evaluate((node) => {
    node.scrollTop = node.scrollHeight;
    node.dispatchEvent(new Event('scroll'));
  });
  const last = page.getByTestId('document-list-item').last();
  await last.click();
  await body.press('ArrowDown');
  await expect(page.getByText('105 loaded')).toBeVisible();
  await expect(page.getByTestId('document-list-item').filter({ hasText: 'document-100.pdf' })).toHaveAttribute('data-active', 'true');

  await page.getByTestId('document-list-search').fill('needle-final');
  await expect(page.getByText('1 loaded')).toBeVisible();
  const match = page.getByTestId('document-list-item');
  await expect(match).toHaveCount(1);
  await expect(match).toContainText('needle-final.pdf');
  await match.click();
  await expect(page.getByTestId('document-reader-chip')).toContainText(/pdf/i);
});
