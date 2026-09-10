// resolve-actions guardrail-1 live demo: the four RESOLVE transforms
// (substitute / replace / combine / fill_missing) driven end-to-end against
// the real stack — ribbon transform tab → RESOLVE group → drawer → commit —
// asserting the committed {input_column}_clean columns via the sheet data API
// (the grid is a canvas; this is the cluster specs' established pattern).
//
// The four tests share ONE seeded project (a messy employer-style CSV) and
// each writes its own output column (employer_clean / dept_clean /
// brand_clean / office_clean), so they never collide. The file opts out of
// fullyParallel (mode: 'default') so the tests run in order in one worker and
// the shared seed is created once; a retried test lands in a fresh worker and
// re-seeds, which is fine because every test only depends on its own input
// column.

import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAction,
  openProject,
  revealRibbonAction,
  sheetData,
  uniqueName,
} from './helpers';

test.describe.configure({ mode: 'default' });

// 12 messy rows. Column roles:
//   employer → Substitute (variant spellings + a blank)
//   brand    → Combine (three Acme variants to hand-group + two keepers)
//   dept     → Replace (code values + a blank)
//   office   → Fill missing (blanks that fill down from above)
const CSV =
  'employer,brand,dept,office\n' +
  '"Acme Corp","Acme Corp","SLS-01","New York"\n' +
  '"ACME CORP.","ACME CORP.","SLS-02",""\n' +
  '"Acme Corp","Widget Co","ENG",""\n' +
  '"Acme, Inc.","Acme Corp","SLS-01","Chicago"\n' +
  '"Globex","Acme, Inc.","OPS",""\n' +
  '"Widget Co","Widget Co","ENG","Chicago"\n' +
  '"ACME CORP.","Hooli","SLS-03",""\n' +
  '"Acme Corp","ACME CORP.","OPS","Boston"\n' +
  '"Globex","Acme, Inc.","ENG",""\n' +
  '"Widget Co","Acme Corp","","Boston"\n' +
  '"Acme, Inc.","Hooli","OPS",""\n' +
  '"","Widget Co","ENG","Denver"\n';

// One project per worker (tests run in order in one worker — see configure
// above). A retry restarts the worker and re-seeds fresh.
let seeded: { pid: string; sheetId: number } | null = null;
async function seedProject(request: APIRequestContext): Promise<{ pid: string; sheetId: number }> {
  if (!seeded) {
    const pid = await createProject(request, uniqueName('e2e-resolve-actions'));
    const sheetId = await importCsv(request, pid, 'employers.csv', CSV);
    seeded = { pid, sheetId };
  }
  return seeded;
}

/** Normalize a wire cell: null / undefined / '' all read as "blank". */
const norm = (value: unknown): string | null =>
  value === null || value === undefined || value === '' ? null : String(value);

/** Poll the sheet API until the named output column exists, then resolve its
 *  values in row order. */
async function pollColumnValues(
  page: Page,
  pid: string,
  sheetId: number,
  columnName: string,
  expected: (string | null)[],
): Promise<void> {
  await expect
    .poll(
      async () => {
        const data = await sheetData(page.request, pid, sheetId);
        const column = data.columns.find((c) => c.name === columnName);
        if (!column) return null;
        return data.rows.map((row) => norm(row.cells[String(column.id)]));
      },
      { timeout: 30_000 },
    )
    .toEqual(expected);
}

test('substitute maps values (one to a target, one to null) and autocloses on Apply', async ({
  page,
}) => {
  const { pid, sheetId } = await seedProject(page.request);
  await openProject(page, pid, sheetId);

  await (await revealRibbonAction(page, 'resolve.substitute')).click();
  const drawer = page.getByTestId('action-drawer');
  await expect(drawer).toBeVisible();
  const form = drawer.getByTestId('resolve-substitute-form');
  await expect(form).toBeVisible();
  await expect(form.getByTestId('resolve-substitute-column')).toHaveValue('employer');
  // the enumeration lands: one row per distinct value
  await expect(form.getByTestId('resolve-substitute-row').first()).toBeVisible();

  // "ACME CORP." → "Acme Corp"
  const acmeRow = form.locator(
    '[data-testid="resolve-substitute-row"][data-value="ACME CORP."]',
  );
  await acmeRow.getByTestId('resolve-substitute-target-input').fill('Acme Corp');
  // "Globex" → (null)
  const globexRow = form.locator('[data-testid="resolve-substitute-row"][data-value="Globex"]');
  await globexRow.getByTestId('resolve-substitute-null-toggle').click();
  await expect(globexRow.getByTestId('resolve-substitute-null-chip')).toBeVisible();

  // 5 distinct non-missing employer values (the blank cell counts as missing)
  await expect(form.getByTestId('resolve-footer-summary')).toContainText('2 of 5 values mapped');
  await page.screenshot({ path: test.info().outputPath('substitute.png') });

  await form.getByTestId('resolve-apply').click();
  // action-drawer-autoclose-on-start-v1: a synchronous completed resolve
  // commit closes the drawer on its own.
  await expect(page.getByTestId('action-drawer')).toHaveCount(0, { timeout: 15_000 });

  await pollColumnValues(page, pid, sheetId, 'employer_clean', [
    'Acme Corp', // kept (unmatched → keep)
    'Acme Corp', // substituted from ACME CORP.
    'Acme Corp',
    'Acme, Inc.', // kept
    null, // Globex → (null)
    'Widget Co', // kept
    'Acme Corp', // substituted
    'Acme Corp',
    null, // Globex → (null)
    'Widget Co',
    'Acme, Inc.',
    null, // missing source cell stays null
  ]);
});

test('replace authors ordered rules with a first-match-wins commit', async ({
  page,
}) => {
  const { pid, sheetId } = await seedProject(page.request);
  await openProject(page, pid, sheetId);

  await (await revealRibbonAction(page, 'resolve.replace')).click();
  const drawer = page.getByTestId('action-drawer');
  const form = drawer.getByTestId('resolve-replace-form');
  await expect(form).toBeVisible();
  await form.getByTestId('resolve-replace-column').selectOption('dept');

  // rule 1: contains "SLS" → "Sales" (the starter blank rule)
  const rules = form.getByTestId('resolve-replace-rule');
  await rules.nth(0).getByTestId('resolve-replace-rule-pattern').fill('SLS');
  await rules.nth(0).getByTestId('resolve-replace-rule-target').fill('Sales');

  // rule 2: exact "SLS-01" → null — fully shadowed by rule 1, so its count
  // proves first-match-wins.
  await form.getByTestId('resolve-replace-add-rule').click();
  await rules.nth(1).getByTestId('resolve-replace-rule-match').selectOption('exact');
  await rules.nth(1).getByTestId('resolve-replace-rule-pattern').fill('SLS-01');
  await rules.nth(1).getByTestId('resolve-replace-rule-null').click();
  await expect(rules.nth(1).getByTestId('resolve-replace-rule-target-null')).toBeVisible();

  // per-rule counts (server-evaluated, first-match-wins disjoint)
  const counts = form.getByTestId('resolve-replace-rule-count');
  await expect(counts).toHaveCount(2);
  await expect(counts.nth(0)).toHaveText('catches 4 rows');
  await expect(counts.nth(1)).toHaveText('catches 0 rows'); // shadowed by rule 1

  await page.screenshot({ path: test.info().outputPath('replace.png') });

  await form.getByTestId('resolve-apply').click();
  await expect(page.getByTestId('action-drawer')).toHaveCount(0, { timeout: 15_000 });

  await pollColumnValues(page, pid, sheetId, 'dept_clean', [
    'Sales', // SLS-01 — rule 1 wins over the exact→null rule
    'Sales', // SLS-02
    'ENG', // no match → keep
    'Sales', // SLS-01 again
    'OPS',
    'ENG',
    'Sales', // SLS-03
    'OPS',
    'ENG',
    null, // blank source cell stays null
    'OPS',
    'ENG',
  ]);
});

test('combine hand-groups three variants into one canonical bucket', async ({ page }) => {
  const { pid, sheetId } = await seedProject(page.request);
  await openProject(page, pid, sheetId);

  await (await revealRibbonAction(page, 'resolve.combine')).click();
  const drawer = page.getByTestId('action-drawer');
  const form = drawer.getByTestId('resolve-combine-form');
  await expect(form).toBeVisible();
  await form.getByTestId('resolve-combine-column-select').selectOption('brand');
  const valueList = form.getByTestId('resolve-combine-unassigned');
  await expect(valueList.getByTestId('resolve-value-row').first()).toBeVisible();

  // click + click selects two variants (checkbox model: no clearing)
  await valueList.locator('[data-testid="resolve-value-row"][data-value="Acme Corp"]').click();
  await valueList.locator('[data-testid="resolve-value-row"][data-value="ACME CORP."]').click();

  await form.getByTestId('resolve-combine-new-bucket').click();
  const bucket = form.getByTestId('resolve-combine-bucket');
  await expect(bucket).toHaveCount(1);
  // the fresh bucket's name input is auto-focused with the most frequent
  // member preselected — type the canonical and confirm with Enter
  const nameInput = bucket.getByTestId('resolve-group-canonical-input');
  await expect(nameInput).toHaveValue('Acme Corp');
  await nameInput.fill('Acme');
  await nameInput.press('Enter');
  await expect(nameInput).toHaveValue('Acme');

  // add the third variant through the bucket's add-value search
  await bucket.getByTestId('resolve-combine-bucket-add-value-input').fill('Inc');
  const option = bucket.getByTestId('resolve-combine-bucket-add-value-option');
  await expect(option).toHaveCount(1);
  await expect(option).toContainText('Acme, Inc.');
  await option.click();
  await expect(bucket.getByTestId('resolve-group-member')).toHaveCount(3);

  await expect(form.getByTestId('resolve-footer-summary')).toContainText('3 → 1 group');
  await page.screenshot({ path: test.info().outputPath('combine.png') });

  await form.getByTestId('resolve-apply').click();
  await expect(page.getByTestId('action-drawer')).toHaveCount(0, { timeout: 15_000 });

  await pollColumnValues(page, pid, sheetId, 'brand_clean', [
    'Acme', // Acme Corp → canonical
    'Acme', // ACME CORP. → canonical
    'Widget Co', // ungrouped → kept
    'Acme',
    'Acme', // Acme, Inc. → canonical (added via bucket search)
    'Widget Co',
    'Hooli', // ungrouped → kept
    'Acme',
    'Acme',
    'Acme',
    'Hooli',
    'Widget Co',
  ]);
});

test('fill missing fills blanks down from above via the generated form', async ({ page }) => {
  const { pid, sheetId } = await seedProject(page.request);
  await openProject(page, pid, sheetId);

  await openAction(page, 'resolve.fill_missing');
  const drawer = page.getByTestId('action-drawer');
  await expect(drawer).toBeVisible();

  // pick the column with blanks; the output name re-seeds to office_clean
  await drawer.getByTestId('field-source').selectOption('office');
  await expect(drawer.getByTestId('field-output-cleaned')).toHaveValue('office_clean');
  // method: fill down
  await drawer.getByTestId('field-method').selectOption('down');

  await page.screenshot({ path: test.info().outputPath('fill.png') });

  await drawer.getByTestId('generated-action-run').click();
  await expect(page.getByTestId('action-drawer')).toHaveCount(0, { timeout: 15_000 });

  await pollColumnValues(page, pid, sheetId, 'office_clean', [
    'New York',
    'New York', // filled from above
    'New York', // filled from above
    'Chicago',
    'Chicago', // filled
    'Chicago',
    'Chicago', // filled
    'Boston',
    'Boston', // filled
    'Boston',
    'Boston', // filled
    'Denver',
  ]);

  // demo shot of the final grid with the committed columns
  await expect(page.getByTestId('grid')).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('grid-after.png') });
});
