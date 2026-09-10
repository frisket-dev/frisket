// Bottom-dock history panel: the op log lives solely in the bottom dock as a
// list + right-hand detail pane (the shared list/detail template). Selecting a
// step shows its detail on the right; Restore performs the step-to; the
// status-bar undo/redo affordances keep working.
// Deterministic: the only ops are an import and a $0 template run.

import { expect, test } from '@playwright/test';
import {
  addRow,
  createProject,
  importCsv,
  openHistory,
  openProject,
  runAndWait,
  TINY_CSV,
  uniqueName,
} from './helpers';

test('op history is a bottom-dock list/detail panel with working step-to/undo/redo', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-history'));
  const sheetId = await importCsv(request, pid, 'stories.csv', TINY_CSV);
  // A deterministic, $0 map op so the log has an undoable step.
  await runAndWait(request, pid, {
    action_id: 'map.template',
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: { template: { text: 'note: {{snippet}}' } },
    output_names: { rendered: 'note' },
    idempotency_key: `e2e-history-template-${pid}`,
  });
  const historyResponse = await request.get(`/api/projects/${pid}/history?limit=50`);
  expect(historyResponse.ok()).toBeTruthy();
  const history = await historyResponse.json() as { ops: Array<{ id: number }>; total: number };
  const templateOpId = history.ops.at(-1)?.id;
  expect(typeof templateOpId).toBe('number');

  const operationActions: Array<Record<string, unknown>> = [];
  const legacyOperationCalls: string[] = [];
  await page.route(`**/api/projects/${pid}/undo`, async (route) => {
    legacyOperationCalls.push(route.request().url());
    await route.fulfill({
      status: 599,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'legacy /undo route should not be called' }),
    });
  });
  await page.route(`**/api/projects/${pid}/redo`, async (route) => {
    legacyOperationCalls.push(route.request().url());
    await route.fulfill({
      status: 599,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'legacy /redo route should not be called' }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    if (payload.action_id === 'operation.undo' || payload.action_id === 'operation.redo') {
      operationActions.push(payload);
    }
    await route.continue();
  });

  const historyPageResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === 'GET' &&
      url.pathname.includes(`/api/projects/${pid}/history`) &&
      url.searchParams.get('limit') === '50';
  });
  await openProject(page, pid, sheetId);
  const historyPage = await (await historyPageResponse).json();
  expect(historyPage.total).toBe(history.total);
  expect(historyPage.ops.length).toBeLessThanOrEqual(50);

  // History is a bottom-dock tab; activate it to render the log.
  await openHistory(page);
  const panel = page.getByTestId('bottom-dock-panel');
  const contribution = page.getByTestId('workbench-contribution-frisket-core-panel-history');
  await expect(contribution).toBeVisible();
  await expect(contribution).toHaveAttribute('data-schema-version', 'frisket.workbench.panel.v1');
  await expect(contribution).toHaveAttribute('data-contribution-id', 'frisket.core.panel.history');
  await expect(contribution).toHaveAttribute('data-host', 'bottomDock');
  await expect(contribution).toHaveAttribute('data-mode', 'tab');
  await expect(contribution).toHaveAttribute(
    'data-runtime-component-key',
    'core.panels.HistoryPanel',
  );
  const historyCapabilities = (await contribution.getAttribute('data-required-capabilities'))
    ?.split(' ') ?? [];
  expect(historyCapabilities).toEqual(expect.arrayContaining([
    'history.list',
    'history.stepTo',
    'operation.undo',
    'operation.redo',
  ]));

  // Both ops render as rows, newest carrying the pointer.
  const step0 = panel.getByTestId('history-step-op-1');
  const step1 = panel.getByTestId(`history-step-op-${templateOpId}`);
  await expect(step0).toContainText(/import/i);
  await expect(step1).toContainText(/template|note/i);
  await expect(step1).toHaveClass(/history-state-current/);

  // Status-bar affordances: the map op is undoable, nothing to redo yet.
  await expect(page.getByTestId('undo-button')).toBeEnabled();
  await expect(page.getByTestId('redo-button')).toBeDisabled();

  // The current op is selected by default, so its detail shows on the right with
  // Restore disabled (already the pointer).
  const currentDetail = panel.getByTestId(`history-step-detail-op-${templateOpId}`);
  await expect(currentDetail).toBeVisible();
  await expect(panel.getByTestId(`history-restore-op-${templateOpId}`)).toBeDisabled();

  // Clicking a step selects it and shows its detail on the right. It does not
  // move the pointer until Restore is explicitly activated.
  await step0.click();
  await expect(step0).toHaveClass(/bottom-dock-row-selected/);
  await expect(step0.locator('.bottom-dock-row-button')).toHaveAttribute('aria-pressed', 'true');
  await expect(panel.getByTestId('history-step-detail-op-1')).toBeVisible();
  await expect.poll(() => operationActions.length, { timeout: 500 }).toBe(0);
  await expect(step1).toHaveClass(/history-state-current/);
  await expect(page.getByTestId('redo-button')).toBeDisabled();

  // Step back to the import via the explicit restore action: the template op
  // shows as undone (kept, not deleted) and redo lights up.
  await panel.getByTestId('history-restore-op-1').click();
  await expect.poll(() => operationActions.filter((a) => a.action_id === 'operation.undo').length).toBe(1);
  expect(operationActions.find((a) => a.action_id === 'operation.undo')).toMatchObject({
    action_id: 'operation.undo',
    scope: { kind: 'project' },
    params: { expected_op_id: templateOpId },
    output_names: {},
  });
  expect(String(operationActions.find((a) => a.action_id === 'operation.undo')?.idempotency_key))
    .toMatch(/^web-operation\.undo:/);
  await expect(step1).toHaveClass(/history-state-undone/);
  await expect(step0).toHaveClass(/history-state-current/);
  await expect(page.getByTestId('redo-button')).toBeEnabled();

  // Redo from the status bar moves the pointer forward again.
  await page.getByTestId('redo-button').click();
  await expect.poll(() => operationActions.filter((a) => a.action_id === 'operation.redo').length).toBe(1);
  expect(operationActions.find((a) => a.action_id === 'operation.redo')).toMatchObject({
    action_id: 'operation.redo',
    scope: { kind: 'project' },
    params: { expected_op_id: templateOpId },
    output_names: {},
  });
  expect(String(operationActions.find((a) => a.action_id === 'operation.redo')?.idempotency_key))
    .toMatch(/^web-operation\.redo:/);
  await expect(step1).toHaveClass(/history-state-current/);
  await expect(page.getByTestId('redo-button')).toBeDisabled();

  // Keyboard activation on a step also selects first; keyboard activation on
  // Restore performs the step-to operation.
  await step0.locator('.bottom-dock-row-button').focus();
  await page.keyboard.press('Enter');
  await expect(step0).toHaveClass(/bottom-dock-row-selected/);
  await expect(panel.getByTestId('history-step-detail-op-1')).toBeVisible();
  await expect.poll(() => operationActions.filter((a) => a.action_id === 'operation.undo').length, {
    timeout: 500,
  }).toBe(1);
  await panel.getByTestId('history-restore-op-1').focus();
  await page.keyboard.press('Enter');
  await expect.poll(() => operationActions.filter((a) => a.action_id === 'operation.undo').length).toBe(2);
  await expect(step1).toHaveClass(/history-state-undone/);
  expect(legacyOperationCalls).toEqual([]);

  // Switching to another dock tab and back preserves the log.
  await page.getByTestId('bottom-dock-tab-jobs').click();
  await expect(step0).not.toBeVisible();
  await page.getByTestId('bottom-dock-tab-history').click();
  await expect(step0).toBeVisible();
});

test('history panel navigates older and newer pages in a long op log', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-history-long'));
  const sheetId = await importCsv(request, pid, 'long-history.csv', TINY_CSV);
  for (let i = 0; i < 75; i += 1) {
    await addRow(request, pid, sheetId, { snippet: `history row ${i}` });
  }

  const firstHistoryResponse = await request.get(`/api/projects/${pid}/history?limit=50`);
  expect(firstHistoryResponse.ok()).toBeTruthy();
  const firstHistory = await firstHistoryResponse.json() as {
    ops: Array<{ id: number }>;
    total: number;
    has_more_before: boolean;
  };
  expect(firstHistory.total).toBeGreaterThan(50);
  expect(firstHistory.has_more_before).toBe(true);
  const newestOpId = firstHistory.ops.at(-1)?.id;
  expect(typeof newestOpId).toBe('number');

  const historyRequests: string[] = [];
  await page.route(`**/api/projects/${pid}/history**`, async (route) => {
    historyRequests.push(route.request().url());
    await route.continue();
  });

  await openProject(page, pid, sheetId);
  await openHistory(page);
  const panel = page.getByTestId('bottom-dock-panel');
  await expect(panel.getByTestId('history-step-op-1')).not.toBeVisible();
  await expect(panel.getByTestId(`history-step-op-${newestOpId}`)).toBeVisible();

  historyRequests.length = 0;
  await panel.getByTestId('history-load-older').click();
  await expect.poll(() => historyRequests.some((url) => {
    const parsed = new URL(url);
    return parsed.searchParams.get('offset') === '0' &&
      parsed.searchParams.get('limit') === '50';
  })).toBe(true);
  await expect(panel.getByTestId('history-step-op-1')).toBeVisible();
  await expect(panel.getByTestId(`history-step-op-${newestOpId}`)).not.toBeVisible();

  historyRequests.length = 0;
  await panel.getByTestId('history-load-newer').click();
  await expect.poll(() => historyRequests.some((url) => {
    const parsed = new URL(url);
    return parsed.searchParams.get('offset') === '50' &&
      parsed.searchParams.get('limit') === '50';
  })).toBe(true);
  await expect(panel.getByTestId('history-step-op-1')).not.toBeVisible();
  await expect(panel.getByTestId(`history-step-op-${newestOpId}`)).toBeVisible();
});
