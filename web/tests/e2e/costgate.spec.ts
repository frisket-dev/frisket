// The cost gate (HTTP 402): an expensive model run reaches the real
// /actions/v1/run gate before any tokens are spent, and the modal the user sees
// is that 402, rendered. The run is NEVER confirmed here - we assert the
// type-to-confirm flow enables the button, then cancel.
//
// This spec used to fight the client for the right to test the server: it
// patched requires_confirmation off the catalog and waited for a priced
// estimate to render, because otherwise the panel's own preflight would open
// its own modal and POST confirmed:true, bypassing the gate this file exists to
// prove. Both workarounds are gone with the preflight. Every launch POSTs
// unconfirmed; the only thing that can raise this modal is a real 402.

import { expect, test, type Locator } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAction,
  uniqueName,
} from './helpers';

const OPUS = 'anthropic/claude-opus-4-8';

const bigCsv = (rows: number): string =>
  'snippet\n' +
  Array.from(
    { length: rows },
    (_, i) => `"City agency item ${i}: routine procurement note for audit review."`,
  ).join('\n');

async function expectCostGateContribution(contribution: Locator) {
  await expect(contribution).toHaveAttribute(
    'data-schema-version',
    'frisket.workbench.view.v1',
  );
  await expect(contribution).toHaveAttribute(
    'data-contribution-id',
    'frisket.core.view.cost_gate',
  );
  await expect(contribution).toHaveAttribute('data-host', 'modalOrPeek');
  await expect(contribution).toHaveAttribute('data-mode', 'peek');
  await expect(contribution).toHaveAttribute(
    'data-runtime-component-key',
    'core.views.CostGateModal',
  );
  const capabilitiesAttr = await contribution.getAttribute('data-required-capabilities');
  expect(capabilitiesAttr).not.toBeNull();
  expect(capabilitiesAttr?.split(/\s+/).filter(Boolean).sort()).toEqual([
    'action.cost.confirm',
    'action.run',
  ]);
}

test('expensive run trips the 402 cost gate modal', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-costgate'));
  await importCsv(page.request, pid, 'big.csv', bigCsv(250));

  // The POST /run request goes to the live backend and must return the V1
  // action-result 402 envelope.
  const runPosts: Array<Record<string, unknown>> = [];
  const estimateRoute = `**/api/projects/${pid}/actions/v1/estimate`;
  const runRoute = `**/api/projects/${pid}/actions/v1/run`;
  const providersRoute = '**/api/providers';
  await page.route(providersRoute, (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      schemaVersion: 'frisket.providers.v1',
      tier: 'local',
      providers: [{
        id: 'anthropic',
        label: 'Anthropic',
        kind: 'platform_api',
        configured: true,
        source: 'env',
        hint: null,
        models: [{ id: OPUS, label: 'Claude Opus 4.8', price: null }],
      }],
    }),
  }));
  await page.route(estimateRoute, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_estimate_result.v1',
        action: { kind: 'map.classify', action_id: 'costgate-estimate-e2e' },
        project_id: pid,
        estimate: {
          rows: 250,
          cost: 0.05,
          cost_source: 'estimated',
          billed_cost: 50_000,
          policy_id: 'frisket.pricing.identity.v1',
          avg_input_tokens: 12,
        },
      }),
    });
  });
  await page.route(runRoute, async (route) => {
    runPosts.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.continue();
  });

  try {
    await page.goto(`/p/${pid}`);
    await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId('sheet-stats')).toHaveText(/250 rows/);

    await openAction(page, 'map.classify');
    await page.getByLabel('Field 1 labels').fill('routine, investigate');

    // Supply Opus through the same provider catalog as production, then pick
    // it through the visible control a user operates.
    await page.getByTestId('model-picker-button').click();
    await page.getByTestId('model-picker-search').fill(OPUS);
    await page.getByTestId('model-option-anthropic-claude-opus-4-8').click();
    await expect(page.getByTestId('model-picker-button')).toContainText('Claude Opus 4.8');

    // No wait for the panel's estimate to land: whether it has or not, the
    // click POSTs an unconfirmed run. A former load-dependent flake came from
    // the preflight racing the
    // debounced estimate; there is no race left to lose.
    await page.getByTestId('run-button').click();

    // POST /run answers 402 -> modal with the SERVER's estimate.
    const modal = page.getByTestId('cost-gate-modal');
    await expect(modal).toBeVisible({ timeout: 20_000 });
    await expectCostGateContribution(
      page.getByTestId('workbench-contribution-frisket-core-view-cost-gate'),
    );
    await expect(page.getByTestId('cost-gate-estimate')).toContainText('$');

    // Run stays locked until the user types "confirm".
    const confirm = page.getByTestId('cost-gate-confirm');
    await expect(confirm).toBeDisabled();
    await page.getByTestId('cost-gate-input').fill('confirm');
    await expect(confirm).toBeEnabled();

    // Never actually confirm; cancel instead.
    await page.getByTestId('cost-gate-cancel').click();
    await expect(modal).toBeHidden();
    expect(runPosts).toHaveLength(1);
    const posted = runPosts[0] as { params: { confirmed?: boolean } };
    expect(posted.params.confirmed ?? false).toBe(false);
  } finally {
    await page.unroute(estimateRoute);
    await page.unroute(runRoute);
    await page.unroute(providersRoute);
  }
});
