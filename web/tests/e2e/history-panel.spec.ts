// Bottom-dock history panel: the op log lives solely in the bottom dock as a
// list + right-hand detail pane (the shared list/detail template). Selecting a
// step shows its detail on the right; Revert/Restore affects only that Change; the
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

test('op history is a bottom-dock list/detail panel with selective Revert/Restore and ordinary Undo/Redo', async ({
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
  const history = await historyResponse.json() as { changes: Array<{ id: number }>; total: number };
  const templateOpId = history.changes.at(-1)?.id;
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
    if (String(payload.action_id).startsWith('operation.')) {
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
  expect(historyPage.changes.length).toBeLessThanOrEqual(50);

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
    'change.revert',
    'change.restore',
    'change.undo',
    'change.redo',
  ]));

  // Both ops render as rows, newest carrying the pointer.
  const step0 = panel.getByTestId('history-step-op-1');
  const step1 = panel.getByTestId(`history-step-op-${templateOpId}`);
  await expect(step0).toContainText(/import/i);
  await expect(step1).toContainText(/template|note/i);
  await expect(step1).toHaveClass(/history-state-applied/);

  // Status-bar affordances: the map op is undoable, nothing to redo yet.
  await expect(page.getByTestId('undo-button')).toBeEnabled();
  await expect(page.getByTestId('redo-button')).toBeDisabled();

  // Inspecting an old entry does not navigate history or change any data.
  await expect(panel.getByTestId(`history-revert-op-${templateOpId}`)).toBeEnabled();
  await step0.click();
  await expect(panel.getByTestId('history-step-detail-op-1')).toBeVisible();
  expect(operationActions).toHaveLength(0);
  await step1.click();
  await panel.getByTestId(`history-revert-op-${templateOpId}`).click();
  await expect(step1).toHaveClass(/history-state-undone/);
  expect(operationActions[0]).toMatchObject({
    action_id: 'change.revert', scope: { kind: 'project' }, params: { change_id: templateOpId },
  });
  await expect(page.getByTestId('grid')).toBeVisible();
  const resize = await page.getByRole('button', { name: 'Resize bottom dock', exact: true }).boundingBox();
  if (!resize) throw new Error('History dock resize handle is missing');
  await page.mouse.move(resize.x + resize.width / 2, resize.y + resize.height / 2);
  await page.mouse.down();
  await page.mouse.move(resize.x + resize.width / 2, resize.y - 180, { steps: 5 });
  await page.mouse.up();
  await page.screenshot({ path: 'test-results/domain-history-restore.png', fullPage: true });
  // Selective Revert is out of band: it does not create a normal Redo step.
  await expect(page.getByTestId('redo-button')).toBeDisabled();
  await panel.getByTestId(`history-restore-op-${templateOpId}`).focus();
  await page.keyboard.press('Enter');
  await expect(step1).toHaveClass(/history-state-applied/);
  expect(operationActions[1]).toMatchObject({
    action_id: 'change.restore', params: { change_id: templateOpId },
  });
  await page.getByTestId('undo-button').click();
  await expect(step1).toHaveClass(/history-state-undone/);
  await expect(page.getByTestId('redo-button')).toBeEnabled();
  await page.getByTestId('redo-button').click();
  await expect(step1).toHaveClass(/history-state-applied/);
  expect(operationActions.map((action) => action.action_id)).toEqual([
    'change.revert', 'change.restore', 'change.undo', 'change.redo',
  ]);
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
    changes: Array<{ id: number }>;
    total: number;
    has_more_before: boolean;
  };
  expect(firstHistory.total).toBeGreaterThan(50);
  expect(firstHistory.has_more_before).toBe(true);
  const newestOpId = firstHistory.changes.at(-1)?.id;
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
