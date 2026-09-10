// Executable companion to COMBINE_VALUES_WALKTHROUGH. Each Playwright step
// takes its title and instruction from the same authored definition rendered
// in the in-product tour and Markdown guide, so the happy-path report remains
// a readable walkthrough rather than a parallel set of test-only annotations.

import { expect, test } from '@playwright/test';

import {
  COMBINE_VALUES_WALKTHROUGH,
  type WalkthroughInstruction,
} from '../../src/walkthrough/walkthroughs';
import {
  createProject,
  listSheets,
  openDiscoverTab,
  openProject,
  sheetData,
  uniqueName,
} from './helpers';

function instructionText(instruction: WalkthroughInstruction): string {
  if (typeof instruction === 'string') return instruction;
  return `${instruction.lead} ${instruction.code}${instruction.explanation ? ` ${instruction.explanation}` : ''}`;
}

test('Combine values walkthrough cleans agency names and repairs their counts', async ({
  page,
}) => {
  const completedSteps: string[] = [];
  const walkthroughStep = async (id: string, body: () => Promise<void>) => {
    const step = COMBINE_VALUES_WALKTHROUGH.steps.find((candidate) => candidate.id === id);
    if (!step) throw new Error(`Unknown Combine values walkthrough step: ${id}`);
    await test.step(`${step.title} — ${instructionText(step.instruction)}`, async () => {
      completedSteps.push(id);
      await body();
    });
  };

  const pid = await createProject(page.request, uniqueName('e2e-combine-walkthrough'));
  const seed = await page.request.post(`/api/projects/${pid}/seed-sample`);
  expect(seed.ok()).toBeTruthy();
  const sheets = await listSheets(page.request, pid);
  const agencyPayments = sheets.find((sheet) => sheet.name === 'Agency payments');
  expect(agencyPayments).toBeTruthy();
  await openProject(page, pid);

  await walkthroughStep('open-agency-payments', async () => {
    await page.getByTestId(`workbench-mainView-tab-${agencyPayments!.id}`).click();
    await expect(page.getByTestId(`workbench-mainView-tab-${agencyPayments!.id}`)).toHaveClass(/active/);
  });
  await walkthroughStep('inspect-agency-column', async () => {
    await expect(page.getByTestId('grid-column-agency')).toBeVisible();
  });
  await walkthroughStep('open-raw-facets', async () => {
    await openDiscoverTab(page, 'Facets');
  });
  await walkthroughStep('choose-raw-agency-facet', async () => {
    await expect(page.getByTestId('friendly-facet-agency')).toContainText('12 values');
  });
  await walkthroughStep('review-fragmented-counts', async () => {
    const values = page.getByTestId('facet-values-agency');
    await expect(values.locator('.friendly-checkbox-row')).toHaveCount(12);
    await expect(values).toContainText('Public Works Dept.');
    await expect(values).toContainText('PUBLIC WORKS DEPARTMENT');
    await expect(values).toContainText('Riverton Water Authority');
  });
  await walkthroughStep('open-transform', async () => {
    await page.getByTestId('ribbon-tab-resolve').click();
    await expect(page.getByTestId('ribbon-action-resolve.combine')).toBeVisible();
  });
  await walkthroughStep('open-combine', async () => {
    await page.getByTestId('ribbon-action-resolve.combine').click();
    await expect(page.getByTestId('resolve-combine-form')).toBeVisible();
  });

  const form = page.getByTestId('resolve-combine-form');
  const unassigned = form.getByTestId('resolve-combine-unassigned');
  const selectValues = async (values: string[]) => {
    for (const value of values) {
      await unassigned.locator(`[data-testid="resolve-value-row"][data-value=${JSON.stringify(value)}]`).click();
    }
  };

  await walkthroughStep('choose-agency-column', async () => {
    await form.getByTestId('resolve-combine-column-select').selectOption('agency');
    await expect(unassigned.getByTestId('resolve-value-row').first()).toBeVisible();
  });
  await walkthroughStep('notice-exact-values', async () => {
    await expect(form.getByTestId('resolve-combine-exact-note')).toContainText(
      'including case and spaces',
    );
    await expect(form.getByTestId('resolve-value-ws')).toHaveCount(2);
  });
  await walkthroughStep('select-public-works', async () => {
    await selectValues([
      'Public Works Department',
      'Public Works Dept.',
      'PUBLIC WORKS DEPARTMENT',
      ' Public Works Department',
      'Public Works Department ',
    ]);
  });
  await walkthroughStep('group-public-works', async () => {
    await form.getByTestId('resolve-combine-new-bucket').click();
    await expect(form.getByTestId('resolve-combine-bucket')).toHaveCount(1);
    await expect(form.getByTestId('resolve-group-canonical-input')).toHaveValue(
      'Public Works Department',
    );
  });
  await walkthroughStep('select-water-authority', async () => {
    await selectValues(['Water Authority', 'Riverton Water Authority', 'water authority']);
  });
  await walkthroughStep('group-water-authority', async () => {
    await form.getByTestId('resolve-combine-new-bucket').click();
    await expect(form.getByTestId('resolve-combine-bucket')).toHaveCount(2);
  });
  await walkthroughStep('select-transit-authority', async () => {
    await selectValues(['Transit Authority', 'Riverton Transit Authority', 'Transit authority']);
  });
  await walkthroughStep('group-transit-authority', async () => {
    await form.getByTestId('resolve-combine-new-bucket').click();
    await expect(form.getByTestId('resolve-footer-summary')).toHaveText(
      '11 → 3 groups · 1 kept · 16 rows → agency_clean',
    );
  });
  await walkthroughStep('keep-unmatched-values', async () => {
    await expect(form.getByTestId('resolve-combine-remainder-policy')).toHaveValue('keep');
  });
  await walkthroughStep('apply-agency-groups', async () => {
    await form.getByTestId('resolve-apply').click();
    await expect(page.getByTestId('action-drawer')).toHaveCount(0, { timeout: 15_000 });
  });
  await walkthroughStep('review-clean-column', async () => {
    await expect(page.getByTestId('grid-column-agency_clean')).toBeVisible({ timeout: 20_000 });
    await expect
      .poll(async () => {
        const data = await sheetData(page.request, pid, agencyPayments!.id);
        const column = data.columns.find((candidate) => candidate.name === 'agency_clean');
        if (!column) return null;
        return data.rows.map((row) => String(row.cells[String(column.id)]));
      })
      .toEqual([
        'Public Works Department',
        'Public Works Department',
        'Public Works Department',
        'Public Works Department',
        'Public Works Department',
        'Public Works Department',
        'Water Authority',
        'Water Authority',
        'Water Authority',
        'Water Authority',
        'Transit Authority',
        'Transit Authority',
        'Transit Authority',
        'Transit Authority',
        'Parks Department',
        'Parks Department',
      ]);
  });
  await walkthroughStep('reopen-clean-facets', async () => {
    await openDiscoverTab(page, 'Facets');
  });
  await walkthroughStep('choose-clean-agency-facet', async () => {
    await page.getByTestId('facet-header-agency_clean').click();
    await expect(page.getByTestId('friendly-facet-agency_clean')).toContainText('4 values');
  });
  await walkthroughStep('verify-clean-counts', async () => {
    const values = page.getByTestId('facet-values-agency_clean');
    await expect(values.locator('.friendly-checkbox-row').filter({ hasText: 'Public Works Department' })).toContainText('6');
    await expect(values.locator('.friendly-checkbox-row').filter({ hasText: 'Water Authority' })).toContainText('4');
    await expect(values.locator('.friendly-checkbox-row').filter({ hasText: 'Transit Authority' })).toContainText('4');
    await expect(values.locator('.friendly-checkbox-row').filter({ hasText: 'Parks Department' })).toContainText('2');
  });

  expect(completedSteps).toEqual(COMBINE_VALUES_WALKTHROUGH.steps.map((step) => step.id));
});
