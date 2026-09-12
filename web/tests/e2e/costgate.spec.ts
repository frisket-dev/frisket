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
import {
  actionSelectorResponse,
  stubActionSelectorChoices,
  type SelectorGroupFixture,
} from './selectorChoicesFixture';

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
  await importCsv(page.request, pid, 'big.csv', bigCsv(1_000));

  // The POST /run request goes to the live backend and must return the V1
  // action-result 402 envelope.
  const runPosts: Array<Record<string, unknown>> = [];
  const runRoute = `**/api/projects/${pid}/actions/v1/run`;
  const choices: SelectorGroupFixture[] = [{
    id: 'local',
    label: 'Local',
    choices: [{
      choiceId: 'local-semantic',
      label: 'Local semantic',
      summary: 'Runs on this computer',
      authoredSelection: { kind: 'engine', engine: 'local_semantic' },
    }],
  }, {
    id: 'anthropic',
    label: 'Anthropic',
    choices: [{
      choiceId: 'anthropic-claude-opus-4-8',
      label: 'Claude Opus 4.8',
      summary: 'Hosted model',
      authoredSelection: { kind: 'engine_model', engine: 'llm', model: OPUS },
    }],
  }];
  await stubActionSelectorChoices(page, pid, ({ actionId, field, params }) => {
    expect(actionId).toBe('map.classify');
    expect(field).toBe('engine');
    return actionSelectorResponse({
      projectId: pid,
      actionId,
      field,
      groups: choices,
      currentChoiceId: params.engine === 'llm' ? 'anthropic-claude-opus-4-8' : 'local-semantic',
    });
  });
  await page.route(runRoute, async (route) => {
    runPosts.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.continue();
  });

  try {
    await page.goto(`/p/${pid}`);
    await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId('sheet-stats')).toHaveText(/1,000 rows/);

    await openAction(page, 'map.classify');
    await page.getByLabel('Field 1 labels').fill('routine, investigate');

    // The typed selector owns the exact authored engine + model pair.
    const selector = page.getByTestId('field-engine');
    const trigger = selector.locator('.engine-selector__trigger');
    await trigger.click();
    const dialog = page.getByTestId('engine-selector-dialog');
    await dialog.getByRole('searchbox', { name: 'Search Engine' }).fill('Claude Opus');
    await dialog.locator('[data-engine-selector-choice="anthropic-claude-opus-4-8"]').click();
    await expect(trigger).toContainText('Claude Opus 4.8');

    // No wait for the panel's estimate to land: whether it has or not, the
    // click POSTs an unconfirmed run. A former load-dependent flake came from
    // the preflight racing the
    // debounced estimate; there is no race left to lose.
    await page.getByTestId('generated-action-run').click();
    await expect.poll(() => runPosts).toHaveLength(1);
    expect(runPosts[0]).toMatchObject({
      params: { engine: 'llm', model: OPUS },
    });

    // POST /run answers 402 -> modal with the SERVER's estimate.
    const modal = page.getByTestId('cost-gate-modal');
    await expect(modal).toBeVisible({ timeout: 20_000 });
    await expectCostGateContribution(
      page.getByTestId('workbench-contribution-frisket-core-view-cost-gate'),
    );
    const quotedCost = page.getByTestId('cost-gate-estimate');
    await expect(quotedCost).toContainText('$');
    const quoteText = await quotedCost.innerText();
    const dollars = Number(quoteText.match(/\$([\d,.]+)/)?.[1]?.replaceAll(',', ''));
    expect(dollars).toBeGreaterThan(2);

    // The server-issued confirmation token enables the deliberate retry. This
    // test cancels instead, so it never sends the confirmed execution POST.
    const confirm = page.getByTestId('cost-gate-confirm');
    await expect(confirm).toBeEnabled();

    // Never actually confirm; cancel instead.
    await page.getByTestId('cost-gate-cancel').click();
    await expect(modal).toBeHidden();
    expect(runPosts).toHaveLength(1);
    const posted = runPosts[0] as { params: { confirmed?: boolean } };
    expect(posted.params.confirmed ?? false).toBe(false);
  } finally {
    await page.unroute(runRoute);
  }
});
