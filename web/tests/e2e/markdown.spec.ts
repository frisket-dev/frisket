// Markdown cells: columns
// whose text is clearly markdown get format='markdown' sniffed on import, the
// format is settable via the v1 column.patch action, and the frontend RENDERS
// the markdown — in the grid's cell preview overlay and in the row drawer.
// Mutating spec: creates its own project; no model calls.

import { expect, test } from '@playwright/test';
import {
  clickCell,
  createProject,
  dblclickCell,
  importCsv,
  openCellDrawer,
  openProject,
  patchColumn,
  sheetColumns,
  uniqueName,
} from './helpers';

const MD_CSV = `title,body
one,"# Heading One

**bold** and a [link](https://example.com)

- first item
- second item"
two,"## Heading Two

*italic* text with \`code\`

1. listed"
`;

test('markdown is sniffed on import and rendered in drawer + grid preview', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('md'));
  const sheetId = await importCsv(page.request, pid, 'markdown.csv', MD_CSV);
  const columns = await sheetColumns(page.request, pid, sheetId);

  // backend sniffed the clearly-markdown column; the plain one stayed default
  expect(columns.find((c) => c.name === 'body')?.format).toBe('markdown');
  expect(columns.find((c) => c.name === 'title')?.format ?? null).toBeNull();

  await openProject(page, pid, sheetId);

  // ---- detail drawer: real rendered markdown, not raw syntax ----
  // 'title' is a plain non-AI text column (now in-place editable —
  // grid-in-place-edit-v1), so the floating icon, not Enter, opens the drawer.
  await openCellDrawer(page, columns, 'title', 0);
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  const md = drawer.getByTestId('markdown-value');
  await expect(md.locator('h1')).toHaveText('Heading One');
  await expect(md.locator('strong')).toHaveText('bold');
  await expect(md.locator('li')).toHaveCount(2);
  await expect(md.locator('a')).toHaveAttribute('href', 'https://example.com');
  await expect(md).not.toContainText('**'); // syntax is rendered, not shown
  await page.keyboard.press('Escape');
  await expect(drawer).toBeHidden();

  // ---- grid preview: opening the cell renders markdown in the overlay ----
  await dblclickCell(page, columns, 'body', 0);
  const overlay = page.locator('#portal');
  await expect(overlay.locator('h1')).toHaveText('Heading One');
  await expect(overlay.locator('strong')).toHaveText('bold');
  await page.keyboard.press('Escape');
});

test('format=markdown is settable via v1 column.patch and the drawer re-renders', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('md-patch'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'notes.csv',
    // plain-looking column the sniffer must NOT flag (single weak signal)
    'note\n"see the **quarterly** numbers"\n"nothing fancy here"\n',
  );
  let columns = await sheetColumns(page.request, pid, sheetId);
  const note = columns.find((c) => c.name === 'note');
  expect(note?.format ?? null).toBeNull();

  // user says "this is markdown" -> v1 column.patch
  await patchColumn(page.request, pid, note!.id, { format: 'markdown' });

  // round-trips through sheet data, and the drawer now renders it
  columns = await sheetColumns(page.request, pid, sheetId);
  expect(columns.find((c) => c.name === 'note')?.format).toBe('markdown');
  await openProject(page, pid, sheetId);
  await clickCell(page, columns, 'note', 0);
  await page.keyboard.press('Enter');
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId('markdown-value').locator('strong')).toHaveText('quarterly');
});
