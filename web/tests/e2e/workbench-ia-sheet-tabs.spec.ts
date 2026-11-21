import { expect, test, type Page } from '@playwright/test';
import {
  clickRunButton,
  createProject,
  importCsv,
  listSheets,
  openAction,
  openProject,
  primeMapRowsReplies,
  typedActionRequest,
  type WireSheet,
  uniqueName,
} from './helpers';
import {
  actionSelectorResponse,
  stubActionSelectorChoices,
  type SelectorGroupFixture,
} from './selectorChoicesFixture';
import {
  prepareOwnModelReplay,
  REPLAY_MODEL as DERIVE_MODEL,
} from './replayModelScenario';

// workbench-ia-sheet-tabs-v1 (Workbench IA increment 3): the Navigate region —
// browser-style sheet tabs above the grid; the vertical sidebar sheet list is
// retired. The assertions below preserve the resulting interaction contract.

const TWO_ROW_CSV = 'city\nAlpha\nBravo\n';
const THREE_ROW_CSV = 'name\nOne\nTwo\nThree\n';

test('strip renders one tab per sheet; active tab is distinct and carries a row-count chip', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('sheet-tabs'));
  const sheetA = await importCsv(request, pid, 'a.csv', TWO_ROW_CSV);
  const sheetB = await importCsv(request, pid, 'b.csv', THREE_ROW_CSV);

  await openProject(page, pid, sheetA);

  const strip = page.getByTestId('workbench-mainView-tabs');
  await expect(strip).toBeVisible();

  const tabA = page.getByTestId(`workbench-mainView-tab-${sheetA}`);
  const tabB = page.getByTestId(`workbench-mainView-tab-${sheetB}`);
  await expect(tabA).toBeVisible();
  await expect(tabB).toBeVisible();

  // The active tab is visually distinct (aria-selected + data-active).
  await expect(tabA).toHaveAttribute('aria-selected', 'true');
  await expect(tabA).toHaveAttribute('data-active', 'true');
  await expect(tabB).toHaveAttribute('aria-selected', 'false');

  // The active tab carries a mono row-count chip reflecting the sheet's rows.
  const chipA = page.getByTestId(`workbench-mainView-tab-rowcount-${sheetA}`);
  await expect(chipA).toBeVisible();
  await expect(chipA).toHaveText('2');
  // Inactive tabs do not carry the chip.
  await expect(page.getByTestId(`workbench-mainView-tab-rowcount-${sheetB}`)).toHaveCount(0);
});

test('clicking a tab switches the active sheet (grid content changes)', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('sheet-tabs-switch'));
  const sheetA = await importCsv(request, pid, 'a.csv', TWO_ROW_CSV);
  const sheetB = await importCsv(request, pid, 'b.csv', THREE_ROW_CSV);
  const sheets = await listSheets(request, pid);
  const nameA = sheets.find((s) => s.id === sheetA)!.name;
  const nameB = sheets.find((s) => s.id === sheetB)!.name;

  await openProject(page, pid, sheetA);
  await expect(page.locator('.sheet-title')).toHaveText(nameA);

  await page.getByTestId(`workbench-mainView-tab-${sheetB}`).click();

  await expect(page.getByTestId(`workbench-mainView-tab-${sheetB}`)).toHaveAttribute(
    'aria-selected',
    'true',
  );
  await expect(page.locator('.sheet-title')).toHaveText(nameB);
  // The chip now follows the active sheet (3 rows).
  await expect(page.getByTestId(`workbench-mainView-tab-rowcount-${sheetB}`)).toHaveText('3');
});

// ---------------------------------------------------------------------------
// Derived-sheet marker — driven by REAL SheetMeta.parent data. We materialize a
// genuine child sheet through the v1 map.extract -> derive.table_from_list
// composite (the same model-free, cache-primed path derive-object-list.spec.ts
// uses), so the marker is asserted against a real lineage edge, not a stub.
// (The seeded "People mentioned" fixture is unusable here: scripts/e2e/seed_demo.py
// still POSTs the removed /api/projects/{pid}/derive endpoint, which 404s and
// leaves that project with no child sheet.)

const DERIVE_CSV =
  'episode,description\n' +
  '"Fitness episode","The host reads an AG1 ad with code ROGAN and a Cash App spot offering a signup bonus."\n' +
  '"Tools episode","A Squarespace segment offers 10% off, then the host mentions AG1 again with code ROGAN."\n';

const DERIVE_PROMPT =
  'List every sponsor mentioned in this row. For each sponsor, return the company, any coupon code or discount, and the product being advertised. Only use facts present in the row.';
const REPLAY_MODEL_CHOICES: SelectorGroupFixture[] = [{
  id: 'gemini',
  label: 'Gemini',
  choices: [{
    choiceId: DERIVE_MODEL,
    label: 'Gemini 3.5 Flash-Lite',
    summary: DERIVE_MODEL,
    authoredSelection: { kind: 'engine_model', engine: 'llm', model: DERIVE_MODEL },
  }],
}];

async function exposeReplayModel(page: Page, projectId: string): Promise<void> {
  await stubActionSelectorChoices(page, projectId, ({ actionId, field, params }) => {
    expect(actionId).toBe('map.extract');
    expect(field).toBe('engine');
    const response = actionSelectorResponse({
      projectId,
      actionId,
      field,
      groups: REPLAY_MODEL_CHOICES,
      currentChoiceId: params.engine === 'llm' && params.model === DERIVE_MODEL ? DERIVE_MODEL : null,
    });
    response.default_choice_id = DERIVE_MODEL;
    response.groups[0].choices[0].is_default = true;
    return response;
  });
}

async function selectDescriptionSource(page: Page): Promise<void> {
  const picker = page.getByTestId('text-source-columns');
  await picker.click();
  await page.getByTestId('text-source-columns-menu')
    .getByRole('option', { name: /^description\b/ }).click();
  await page.getByLabel('Filter columns').press('Escape');
  await expect(picker.getByRole('button', { name: 'Remove description' })).toBeVisible();
}

function primeDeriveObjectCache(pid: string, sheetId: number): void {
  const action = typedActionRequest('map.extract', {
    sheetId,
    params: {
      source: ['episode', 'description'],
      model: DERIVE_MODEL,
      instruction: DERIVE_PROMPT,
      fields: [
        {
          name: 'sponsors',
          type: 'list',
          description: 'Sponsors mentioned in this episode',
          items: {
            type: 'object',
            properties: {
              company: { type: 'string', description: 'Sponsor company name' },
              coupon: { type: 'string', description: 'Coupon code or discount' },
              product: { type: 'string', description: 'Product being advertised' },
            },
            required: ['company', 'coupon', 'product'],
          },
        },
      ],
      grounding: { enabled: false },
    },
    outputNames: { sponsors: 'sponsors' },
  });
  primeMapRowsReplies(pid, action, [
    {
      sponsors: [
        { company: 'AG1', coupon: 'ROGAN', product: 'daily greens supplement' },
        { company: 'Cash App', coupon: 'signup bonus', product: 'mobile payments app' },
      ],
    },
    {
      sponsors: [
        { company: 'Squarespace', coupon: '10% off', product: 'website builder' },
        { company: 'AG1', coupon: 'ROGAN', product: 'daily greens supplement' },
      ],
    },
  ]);
}

async function waitForSheet(
  request: Parameters<typeof listSheets>[0],
  pid: string,
  name: string,
): Promise<WireSheet> {
  for (let i = 0; i < 60; i += 1) {
    const sheet = (await listSheets(request, pid)).find((s) => s.name === name);
    if (sheet) return sheet;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`sheet ${name} did not appear`);
}

test('a derived sheet shows the derived marker; a root sheet does not', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('sheet-tabs-derived'));
  await prepareOwnModelReplay(request, pid);
  const rootSheet = await importCsv(request, pid, 'episodes.csv', DERIVE_CSV);
  primeDeriveObjectCache(pid, rootSheet);
  await exposeReplayModel(page, pid);

  await page.goto(`/p/${pid}`);
  await openAction(page, 'derive.table_from_list');
  await page.getByTestId('derive-source-mode-ai').click();
  await page.getByTestId('field-citation_mode').selectOption('none');
  await selectDescriptionSource(page);
  await page.getByTestId('field-instruction').fill(DERIVE_PROMPT);
  await page.getByTestId('output-field-name').fill('sponsors');
  await page.getByTestId('output-field-description').fill('Sponsors mentioned in this episode');
  await page.locator('#derive-target-sheet').fill('Sponsors');

  await expect(page.getByTestId('list-item-schema')).toBeVisible();
  await page.getByTestId('list-item-kind').selectOption('object');
  await page.getByTestId('list-item-field-name').nth(0).fill('company');
  await page.getByTestId('list-item-field-description').nth(0).fill('Sponsor company name');
  await page.getByTestId('list-item-field-add').click();
  await page.getByTestId('list-item-field-name').nth(1).fill('coupon');
  await page.getByTestId('list-item-field-description').nth(1).fill('Coupon code or discount');
  await page.getByTestId('list-item-field-add').click();
  await page.getByTestId('list-item-field-name').nth(2).fill('product');
  await page.getByTestId('list-item-field-description').nth(2).fill('Product being advertised');

  await clickRunButton(page);

  const child = await waitForSheet(request, pid, 'Sponsors');
  // Confirm the lineage edge is real: the child carries parent_sheet_id.
  expect(child.parent_sheet_id).toBe(rootSheet);

  await openProject(page, pid, child.id);

  await expect(page.getByTestId(`workbench-mainView-tab-${child.id}`)).toBeVisible();
  await expect(page.getByTestId(`workbench-mainView-tab-${rootSheet}`)).toBeVisible();
  // Derived sheets render the derived marker glyph; root sheets do NOT.
  await expect(page.getByTestId(`workbench-mainView-tab-derived-${child.id}`)).toBeVisible();
  await expect(page.getByTestId(`workbench-mainView-tab-derived-${rootSheet}`)).toHaveCount(0);
});

test('the trailing + button opens the import dialog', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('sheet-tabs-add'));
  const sheetId = await importCsv(request, pid, 'a.csv', TWO_ROW_CSV);

  await openProject(page, pid, sheetId);

  const addButton = page.getByTestId('workbench-mainView-add-sheet');
  await expect(addButton).toBeVisible();
  await addButton.click();

  const dialog = page.getByTestId('import-workspace-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveJSProperty('open', true);
});

test('the sidebar vertical sheet list is retired', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('sheet-tabs-sidebar'));
  const sheetId = await importCsv(request, pid, 'a.csv', TWO_ROW_CSV);

  await openProject(page, pid, sheetId);

  // The Navigate region lives in the tab strip only — the old sidebar sheet
  // list is gone.
  await expect(page.getByTestId('sheet-tabs')).toHaveCount(0);
  await expect(page.getByTestId(`sheet-tab-${sheetId}`)).toHaveCount(0);
  // Navigation still works through the strip.
  await expect(page.getByTestId(`workbench-mainView-tab-${sheetId}`)).toBeVisible();
});
