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
  openProject,
  sheetData,
  uniqueName,
  openFilterSidebar,
} from './helpers';

function instructionText(instruction: WalkthroughInstruction): string {
  if (typeof instruction === 'string') return instruction;
  return `${instruction.lead} ${instruction.code}${instruction.explanation ? ` ${instruction.explanation}` : ''}`;
}

test('Combine values walkthrough clusters, combines agency names, verifies counts, and joins to Contracts', async ({
  page,
}, testInfo) => {
  const completedSteps: string[] = [];
  const walkthroughStep = async (
    id: string,
    body: () => Promise<void>,
    options: { advancedByFill?: boolean } = {},
  ) => {
    const step = COMBINE_VALUES_WALKTHROUGH.steps.find((candidate) => candidate.id === id);
    if (!step) throw new Error(`Unknown Combine values walkthrough step: ${id}`);
    expect(step).toBe(COMBINE_VALUES_WALKTHROUGH.steps[completedSteps.length]);
    await test.step(`${step.title} — ${instructionText(step.instruction)}`, async () => {
      const card = page.getByTestId('walkthrough-card');
      await expect(card.getByRole('heading', { name: step.title, exact: true })).toBeVisible();
      await expect(card.locator('.walkthrough-progress')).toHaveText(
        `${completedSteps.length + 1} of ${COMBINE_VALUES_WALKTHROUGH.steps.length}`,
      );
      await expect(page.getByTestId('walkthrough-target-box'), `${id} spotlight must resolve`)
        .toBeVisible({ timeout: step.target.kind === 'completed-run' ? 30_000 : 10_000 });
      await page.screenshot({
        path: testInfo.outputPath(`${String(completedSteps.length + 1).padStart(2, '0')}-${id}.png`),
      });
      await body();
      if (step.advance === 'next-button' && !options.advancedByFill) {
        await card.getByRole('button', {
          name: completedSteps.length === COMBINE_VALUES_WALKTHROUGH.steps.length - 1
            ? 'Finish'
            : 'Next',
        }).click();
      }
      completedSteps.push(id);
    });
  };

  const pid = await createProject(page.request, uniqueName('e2e-combine-walkthrough'));
  const seed = await page.request.post(`/api/projects/${pid}/seed-sample`);
  expect(seed.ok()).toBeTruthy();
  const sheets = await listSheets(page.request, pid);
  const agencyPayments = sheets.find((sheet) => sheet.name === 'Agency payments');
  expect(agencyPayments).toBeTruthy();
  await openProject(page, pid);
  await page.getByTestId('chrome-walkthrough').click();
  await expect(page.getByTestId('walkthrough-chooser')).toBeVisible();
  await page.getByTestId('walkthrough-choice-combine-values').click();

  await walkthroughStep('open-agency-payments', async () => {
    await page.getByTestId(`workbench-mainView-tab-${agencyPayments!.id}`).click();
    await expect(page.getByTestId(`workbench-mainView-tab-${agencyPayments!.id}`)).toHaveClass(/active/);
  });
  await walkthroughStep('inspect-agency-column', async () => {
    await expect(page.getByTestId('grid-column-agency')).toBeVisible();
  });
  await walkthroughStep('open-raw-facets', async () => {
    await openFilterSidebar(page);
  });
  await walkthroughStep('choose-raw-agency-facet', async () => {
    await expect(page.getByTestId('friendly-facet-agency')).toContainText('12 values');
  });
  await walkthroughStep('open-transform-cluster', async () => {
    await page.getByTestId('ribbon-tab-transform').click();
    await expect(page.getByTestId('ribbon-action-cluster.values')).toBeVisible();
  });
  await walkthroughStep('open-cluster-values', async () => {
    await page.getByTestId('ribbon-action-cluster.values').click();
    await expect(page.getByTestId('cluster-review-form')).toBeVisible();
  });
  await walkthroughStep('configure-cluster', async () => {
    await page.getByTestId('walkthrough-fill-for-me').click();
    await expect(page.getByTestId('cluster-column-select')).toHaveValue('agency');
    await expect(page.getByTestId('cluster-method-select')).toHaveValue('fingerprint');
  }, { advancedByFill: true });
  await walkthroughStep('preview-clusters', async () => {
    await page.getByTestId('cluster-preview-button').click();
    await expect(page.getByTestId('cluster-card')).toHaveCount(3, { timeout: 15_000 });
  });
  await walkthroughStep('apply-clusters', async () => {
    await page.getByTestId('cluster-commit-button').click();
    await expect(page.getByTestId('action-drawer')).toHaveCount(0, { timeout: 15_000 });
    await expect(page.getByTestId('grid-column-agency_canonical')).toBeVisible({ timeout: 20_000 });
  });

  await walkthroughStep('open-transform-combine', async () => {
    await page.getByTestId('ribbon-tab-transform').click();
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

  await walkthroughStep('choose-agency-canonical', async () => {
    await page.getByTestId('walkthrough-fill-for-me').click();
    await expect(form.getByTestId('resolve-combine-column-select')).toHaveValue('agency_canonical');
    await expect(unassigned.getByTestId('resolve-value-row').first()).toBeVisible();
  }, { advancedByFill: true });
  await walkthroughStep('group-editorial-variants', async () => {
    await selectValues(['Public Works Department', 'Public Works Dept.']);
    await form.getByTestId('resolve-combine-new-bucket').click();
    await selectValues(['Water Authority', 'Riverton Water Authority']);
    await form.getByTestId('resolve-combine-new-bucket').click();
    await selectValues(['Transit Authority', 'Riverton Transit Authority']);
    await form.getByTestId('resolve-combine-new-bucket').click();
    await expect(form.getByTestId('resolve-combine-bucket')).toHaveCount(3);
  });
  await walkthroughStep('apply-agency-groups', async () => {
    await page.getByTestId('resolve-apply').click();
    await expect(page.getByTestId('action-drawer')).toHaveCount(0, { timeout: 15_000 });
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
    // The step advances on a click of the Filter toggle, so start closed.
    const close = page.getByTestId('filter-sidebar-close');
    if (await close.isVisible()) await close.click();
    await openFilterSidebar(page);
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
  await walkthroughStep('open-tables-spend', async () => {
    await page.getByTestId('ribbon-tab-tables').click();
    await expect(page.getByTestId('ribbon-action-derive.join')).toBeVisible();
  });
  await walkthroughStep('open-join-contracts', async () => {
    await page.getByTestId('ribbon-action-derive.join').click();
    await expect(page.getByTestId('tabular-join-form')).toBeVisible();
  });
  await walkthroughStep('configure-agency-join', async () => {
    await expect(page.getByTestId('field-join_right_sheet')).toBeEnabled({ timeout: 10_000 });
    await page.getByTestId('walkthrough-fill-for-me').click();
    await expect(page.getByTestId('field-sheet_name')).toHaveValue('Agency spend with contracts');
  }, { advancedByFill: true });
  await walkthroughStep('run-agency-join', async () => {
    await expect(page.getByTestId('generated-action-run')).toBeEnabled({ timeout: 10_000 });
    await page.getByTestId('generated-action-run').click();
    await expect(page.getByTestId('action-drawer')).toHaveCount(0, { timeout: 15_000 });
  });
  await walkthroughStep('review-agency-spend', async () => {
    await expect(page.getByTestId('grid')).toBeVisible();
  });

  expect(completedSteps).toEqual(COMBINE_VALUES_WALKTHROUGH.steps.map((step) => step.id));
  await expect(page.getByTestId('walkthrough-card')).toHaveCount(0);
});
