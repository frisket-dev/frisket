import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import {
  addRow,
  createProject,
  editCells,
  importCsv,
  listSheets,
  openProject,
  setColumnType,
  sheetColumns,
  sheetData,
  solidTilePng,
  uniqueName,
} from './helpers';

type PluginViewSpyEvent =
  | { kind: 'rowsQuery'; contributionId: string; offset: number; limit: number }
  | { kind: 'mediaFromCell'; contributionId: string; hasValue: boolean }
  | { kind: 'openRow'; contributionId: string; rowId: string };

declare global {
  interface Window {
    __FRISKET_PLUGIN_VIEW_TEST_EVENTS__?: PluginViewSpyEvent[];
    __FRISKET_DISABLED_PLUGIN_VIEW_CAPABILITIES__?: string[];
    __FRISKET_PLUGIN_VIEW_TEST_SPIES__?: {
      rowsQuery?: (args: { contributionId: string; offset: number; limit: number }) => void;
      mediaFromCell?: (args: { contributionId: string; hasValue: boolean }) => void;
      openRow?: (args: { contributionId: string; rowId: string }) => void;
    };
  }
}

const CONTRIBUTION_ID = 'frisket.media.view.image_gallery';
const CONTRIBUTION_TEST_ID = 'workbench-contribution-frisket-media-view-image-gallery';

interface ImageSheetFixture {
  sheetId: number;
  firstRowId: number;
  mediaColumnId: number;
  mediaColumnName: string;
  firstMediaCell: Record<string, unknown>;
}

async function importImageSheet(
  request: APIRequestContext,
  pid: string,
  count = 15,
): Promise<ImageSheetFixture> {
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
  const firstRow = firstPage.rows[0];
  expect(firstRow).toBeTruthy();
  const firstCell = firstRow?.cells[String(mediaColumn!.id)];
  expect(firstCell).toBeTruthy();
  for (let index = 2; index <= count; index += 1) {
    await addRow(request, pid, sheet!.id, {
      media: {
        ...(firstCell as Record<string, unknown>),
        filename: `gallery-${String(index).padStart(2, '0')}.png`,
      },
    });
  }
  return {
    sheetId: sheet!.id,
    firstRowId: firstRow!.id,
    mediaColumnId: mediaColumn!.id,
    mediaColumnName: mediaColumn!.name,
    firstMediaCell: firstCell as Record<string, unknown>,
  };
}

async function installPluginViewSpies(page: Page) {
  await page.addInitScript(() => {
    window.__FRISKET_PLUGIN_VIEW_TEST_EVENTS__ = [];
    window.__FRISKET_PLUGIN_VIEW_TEST_SPIES__ = {
      rowsQuery: (args) => {
        window.__FRISKET_PLUGIN_VIEW_TEST_EVENTS__?.push({ kind: 'rowsQuery', ...args });
      },
      mediaFromCell: (args) => {
        window.__FRISKET_PLUGIN_VIEW_TEST_EVENTS__?.push({ kind: 'mediaFromCell', ...args });
      },
      openRow: (args) => {
        window.__FRISKET_PLUGIN_VIEW_TEST_EVENTS__?.push({ kind: 'openRow', ...args });
      },
    };
  });
}

async function pluginViewEvents(page: Page): Promise<PluginViewSpyEvent[]> {
  return page.evaluate(() => window.__FRISKET_PLUGIN_VIEW_TEST_EVENTS__ ?? []);
}

async function preferColumnOrder(
  page: Page,
  pid: string,
  sheetId: number,
  order: string[],
) {
  await page.addInitScript(
    ({ pid, sheetId, order }) => {
      localStorage.setItem(`frisket:column-order:${pid}:${sheetId}`, JSON.stringify(order));
    },
    { pid, sheetId, order },
  );
}

test('image gallery mounts through PluginViewContext and pages image rows', async ({ page }) => {
  await installPluginViewSpies(page);
  const pid = await createProject(page.request, uniqueName('e2e-plugin-image-gallery'));
  const imageFixture = await importImageSheet(page.request, pid, 15);
  const imageSheetId = imageFixture.sheetId;
  const imageColumns = await sheetColumns(page.request, pid, imageSheetId);
  const filenameColumn = imageColumns.find((column) => column.name === 'filename');
  expect(filenameColumn).toBeTruthy();
  await setColumnType(page.request, pid, filenameColumn!.id, 'image');
  await editCells(page.request, pid, [
    {
      rowId: imageFixture.firstRowId,
      columnId: filenameColumn!.id,
      value: {
        ...imageFixture.firstMediaCell,
        filename: 'visible-first-01.png',
      },
    },
  ]);
  await preferColumnOrder(page, pid, imageSheetId, [
    filenameColumn!.name,
    imageFixture.mediaColumnName,
    ...imageColumns
      .map((column) => column.name)
      .filter((name) => name !== filenameColumn!.name && name !== imageFixture.mediaColumnName),
  ]);
  const textSheetId = await importCsv(
    page.request,
    pid,
    'notes.csv',
    'title\nNo images here\nStill no media\n',
  );

  await openProject(page, pid, imageSheetId);

  const frame = page.getByTestId(CONTRIBUTION_TEST_ID);
  await expect(frame).toBeVisible();
  await expect(frame).toHaveAttribute('data-contribution-id', CONTRIBUTION_ID);
  await expect(frame).toHaveAttribute('data-host', 'mainView');
  await expect(frame).toHaveAttribute('data-mode', 'pane');
  await expect(frame).toHaveAttribute(
    'data-required-capabilities',
    'sheet.rows.read media.blob.resolve host.navigation.openRow',
  );
  await expect(frame).toHaveAttribute(
    'data-plugin-view-context-schema-version',
    'frisket.plugin_view_context.v1',
  );

  const gallery = page.getByTestId('plugin-image-gallery');
  await expect(gallery).toBeVisible();
  await expect(gallery).toHaveAttribute('data-image-column-id', String(filenameColumn!.id));
  await expect(gallery).toHaveAttribute('data-page-size', '12');
  await expect(gallery).toHaveAttribute('data-loaded-row-count', '12');
  const firstImage = frame.locator('img').first();
  await expect(firstImage).toBeVisible();
  await expect(firstImage).toHaveAttribute('src', new RegExp(`/api/projects/${pid}/blobs/`));
  await expect(frame).toContainText('visible-first-01.png');

  let events = await pluginViewEvents(page);
  expect(events).toContainEqual({
    kind: 'rowsQuery',
    contributionId: CONTRIBUTION_ID,
    offset: 0,
    limit: 12,
  });
  expect(events.some((event) => event.kind === 'mediaFromCell' && event.hasValue)).toBeTruthy();

  await page.getByTestId('plugin-image-gallery-load-more').click();
  await expect(gallery).toHaveAttribute('data-loaded-row-count', '15');
  events = await pluginViewEvents(page);
  expect(events).toContainEqual({
    kind: 'rowsQuery',
    contributionId: CONTRIBUTION_ID,
    offset: 12,
    limit: 12,
  });

  await page.getByTestId('plugin-image-gallery-tile').first().click();
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('row-field-filename').locator('img')).toBeVisible();
  events = await pluginViewEvents(page);
  expect(events.some((event) => event.kind === 'openRow')).toBeTruthy();

  await page.getByTestId(`workbench-mainView-tab-${textSheetId}`).click();
  await expect(page.getByTestId('plugin-image-gallery')).toHaveCount(0);
  await expect(page.getByTestId(CONTRIBUTION_TEST_ID)).toHaveCount(0);
  const layoutItem = page.getByTestId('workbench-resolved-layout-item-frisket-media-view-image-gallery');
  await expect(layoutItem).toHaveAttribute(
    'data-reason',
    'data_requirement_unmet:sheetHasColumnType:image',
  );
});

test('missing required host capability prevents gallery mount and exposes reason', async ({
  page,
}) => {
  await page.addInitScript(() => {
    window.__FRISKET_DISABLED_PLUGIN_VIEW_CAPABILITIES__ = ['media.blob.resolve'];
  });
  const pid = await createProject(page.request, uniqueName('e2e-plugin-image-gallery-cap'));
  const imageFixture = await importImageSheet(page.request, pid, 2);
  const imageSheetId = imageFixture.sheetId;

  await openProject(page, pid, imageSheetId);

  await expect(page.getByTestId('plugin-image-gallery')).toHaveCount(0);
  const unavailable = page.getByTestId('plugin-view-unavailable-frisket-media-view-image-gallery');
  await expect(unavailable).toBeVisible();
  await expect(unavailable).toHaveAttribute(
    'data-reason',
    'missing_capability:media.blob.resolve',
  );
  await expect(page.getByTestId(CONTRIBUTION_TEST_ID)).toHaveAttribute(
    'data-plugin-view-unavailable-reason',
    'missing_capability:media.blob.resolve',
  );
});
