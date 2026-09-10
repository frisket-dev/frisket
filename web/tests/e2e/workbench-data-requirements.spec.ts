import { expect, test, type APIRequestContext } from '@playwright/test';
import {
  addRow,
  createProject,
  importCsv,
  listSheets,
  openProject,
  sheetColumns,
  sheetData,
  solidTilePng,
  uniqueName,
} from './helpers';

// Declared dataRequirements are behavioral through the generic first-party feed
// (firstMissingDataRequirement in web/src/workbench/dataRequirements.ts), and
// optional data requirements never disable.
// This pins descriptor metadata honesty at the rendered workbench boundary.

const GALLERY_LAYOUT_ITEM = 'workbench-resolved-layout-item-frisket-media-view-image-gallery';
// The optional-activeCell requirement lives on the evidence view (the evidence
// viewer honestly uses the active cell optionally). It is the first-party
// witness of "an optional data requirement never disables the contribution";
// it moved here when the preview dock tab retired.
const EVIDENCE_LAYOUT_ITEM = 'workbench-resolved-layout-item-frisket-core-view-evidence';

async function importImageSheet(request: APIRequestContext, pid: string): Promise<number> {
  const png = solidTilePng(16, [68, 120, 178, 255]);
  const response = await request.post(`/api/projects/${pid}/import/files?sheet_name=gallery`, {
    multipart: {
      files: { name: 'gallery-01.png', mimeType: 'image/png', buffer: png },
    },
  });
  expect(response.ok()).toBeTruthy();
  const sheets = await listSheets(request, pid);
  const sheet = sheets.find((candidate) => candidate.name === 'gallery');
  expect(sheet).toBeTruthy();
  const columns = await sheetColumns(request, pid, sheet!.id);
  const mediaColumn = columns.find((column) => column.name === 'media');
  expect(mediaColumn).toBeTruthy();
  const firstPage = await sheetData(request, pid, sheet!.id, 0, 1);
  const firstCell = firstPage.rows[0]?.cells[String(mediaColumn!.id)];
  expect(firstCell).toBeTruthy();
  await addRow(request, pid, sheet!.id, {
    media: { ...(firstCell as Record<string, unknown>), filename: 'gallery-02.png' },
  });
  return sheet!.id;
}

test('declared data requirements disable and enable through the generic feed', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-data-requirements'));
  const imageSheetId = await importImageSheet(page.request, pid);
  const textSheetId = await importCsv(
    page.request,
    pid,
    'notes.csv',
    'title\nNo images here\nStill no media\n',
  );

  await openProject(page, pid, textSheetId);

  // Image gallery declares dataRequirements: [{kind: sheetHasColumnType, columnType: image}].
  // On a sheet without an image column the generic feed disables it with the reason.
  const galleryItem = page.getByTestId(GALLERY_LAYOUT_ITEM);
  await expect(galleryItem).toHaveAttribute('data-status', 'disabled');
  await expect(galleryItem).toHaveAttribute(
    'data-reason',
    'data_requirement_unmet:sheetHasColumnType:image',
  );

  // The evidence view declares activeCell as an OPTIONAL data requirement; an
  // optional data requirement is a hint and must never disable the contribution.
  const evidenceItem = page.getByTestId(EVIDENCE_LAYOUT_ITEM).first();
  await expect(evidenceItem).toHaveAttribute('data-status', 'enabled');

  // Switching to a sheet with an image-typed column satisfies the requirement.
  await page.getByTestId(`workbench-mainView-tab-${imageSheetId}`).click();
  await expect(page.getByTestId(GALLERY_LAYOUT_ITEM)).toHaveAttribute(
    'data-status',
    'enabled',
  );
});
