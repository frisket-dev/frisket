import { expect, test, type APIRequestContext } from '@playwright/test';

import {
  clickHeader,
  clickRunButton,
  createProject,
  editCells,
  importCsv,
  listSheets,
  openAction,
  openCellDrawer,
  openProject,
  primeMapRowsReplies,
  runAndWait,
  selectActionSourceColumns,
  sheetColumns,
  sheetData,
  typedActionRequest,
  type ActionRequest,
  type WireColumn,
  uniqueName,
} from './helpers';
import {
  exposeReplayModel,
  prepareOwnModelReplay,
  REPLAY_MODEL as MODEL,
} from './replayModelScenario';
import type { JsonValue } from '../../src/api/types';

const TARGET_TRACE_SENTINEL = 'TARGET_TRACE_SENTINEL_v1_long_lived';
const TARGET_RESPONSE_SENTINEL = 'TARGET_RESPONSE_SENTINEL_v1_long_lived';
const OLD_TRACE_SENTINEL = 'OLD_TRACE_SENTINEL_v1_long_lived';
const OTHER_SHEET_SENTINEL = 'OTHER_SHEET_SENTINEL_v1_long_lived';

const TARGET_PROMPT = `Classify the current target sheet only. ${TARGET_TRACE_SENTINEL}`;
const OLD_PROMPT = `Classify the older sheet only. ${OLD_TRACE_SENTINEL}`;
const OTHER_PROMPT = `Classify the same row index on another sheet only. ${OTHER_SHEET_SENTINEL}`;

type WireColumnWithRun = WireColumn & { latest_run_id?: number | null };

type HistoryStep = {
  id: number;
  kind: string;
  status: string;
};

type ActionRunPost = {
  action_id?: unknown;
  scope?: Record<string, unknown>;
  params?: Record<string, unknown>;
  idempotency_key?: unknown;
};

function csvFor(...snippets: string[]): string {
  return `snippet\n${snippets.map((snippet) => JSON.stringify(snippet)).join('\n')}\n`;
}

function classifyAction(
  sheetId: number,
  prompt: string,
  labels: string[],
  includeExtras: boolean,
): ActionRequest {
  return typedActionRequest('map.classify', {
    sheetId,
    params: {
      source: ['snippet'],
      engine: 'llm',
      model: MODEL,
      context: prompt,
      fields: [
        {
          name: 'topic',
          type: 'category',
          description: 'Best-fit label from the list',
          labels,
        },
      ],
      include_justification: includeExtras,
      include_confidence: includeExtras,
    },
    outputNames: { topic: 'topic' },
  });
}

function classifyReplies(
  topic: string,
  justification: string,
  includeExtras: boolean,
): Array<Record<string, JsonValue>> {
  const reply: Record<string, JsonValue> = includeExtras
    ? { topic, topic_confidence: 0.93, topic_justification: justification }
    : { topic };
  return [reply, reply];
}

async function runDebugClassify(
  request: APIRequestContext,
  pid: string,
  sheetId: number,
  prompt: string,
  topic: string,
  justification: string,
  labels: string[],
): Promise<number> {
  const action = classifyAction(sheetId, prompt, labels, true);
  primeMapRowsReplies(pid, action, classifyReplies(topic, justification, true));
  return runAndWait(request, pid, action);
}

async function history(request: APIRequestContext, pid: string): Promise<HistoryStep[]> {
  const res = await request.get(`/api/projects/${pid}/history?limit=50`);
  expect(res.ok()).toBeTruthy();
  const body = await res.json() as { ops: HistoryStep[] };
  return body.ops;
}

async function undoExpectedOperation(
  request: APIRequestContext,
  pid: string,
  opId: number,
): Promise<void> {
  const res = await request.post(`/api/projects/${pid}/actions/v1/run`, {
    data: {
      action_id: 'operation.undo',
      scope: { kind: 'project' },
      params: { expected_op_id: opId },
      output_names: {},
      idempotency_key: `long-lived-undo@sha256:${pid}-${opId}`,
    },
  });
  expect(res.ok()).toBeTruthy();
  const body = await res.json();
  expect(body.schema_version).toBe('frisket.action_result.v1');
  expect(body.status).toBe('completed');
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value))
    : undefined;
}

function actionPostFrom(data: unknown): ActionRunPost | null {
  const record = asRecord(data);
  if (!record) return null;
  return {
    action_id: record.action_id,
    scope: asRecord(record.scope),
    params: asRecord(record.params),
    idempotency_key: record.idempotency_key,
  };
}

test('v1 web paths keep stable sheet/run/row targets in a long-lived project', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-long-lived-v1'));
  await prepareOwnModelReplay(page.request, pid);

  const oldSheetId = await importCsv(
    page.request,
    pid,
    '01-old-trace.csv',
    csvFor(
      `older sheet row zero ${OLD_TRACE_SENTINEL}`,
      'older sheet row one',
    ),
  );
  await runDebugClassify(
    page.request,
    pid,
    oldSheetId,
    OLD_PROMPT,
    'old',
    OLD_TRACE_SENTINEL,
    ['old', 'other'],
  );
  const oldColumns: WireColumnWithRun[] = await sheetColumns(page.request, pid, oldSheetId);
  const oldTopic = oldColumns.find((column) => column.name === 'topic');
  expect(oldTopic).toBeTruthy();

  const otherSheetId = await importCsv(
    page.request,
    pid,
    '02-other-same-row-index.csv',
    csvFor(
      `same row index on another sheet ${OTHER_SHEET_SENTINEL}`,
      'another sheet row one',
    ),
  );
  await runDebugClassify(
    page.request,
    pid,
    otherSheetId,
    OTHER_PROMPT,
    'other',
    OTHER_SHEET_SENTINEL,
    ['other', 'old'],
  );

  const noiseSheetId = await importCsv(
    page.request,
    pid,
    '03-hidden-output.csv',
    csvFor('temporary output row zero', 'temporary output row one'),
  );
  await runAndWait(page.request, pid, {
    action_id: 'map.template',
    scope: { kind: 'sheet_rows', sheet_id: noiseSheetId },
    params: { template: { text: 'temporary {{snippet}}' } },
    output_names: { rendered: 'topic' },
    idempotency_key: `long-lived-template-${pid}`,
  });
  const templateOpId = (await history(page.request, pid)).at(-1)?.id;
  expect(templateOpId).toBeTruthy();
  await undoExpectedOperation(page.request, pid, Number(templateOpId));
  expect(
    (await history(page.request, pid)).some((step) => (
      step.id === Number(templateOpId) && step.status === 'undone'
    )),
  ).toBe(true);
  await expect
    .poll(async () => (await sheetColumns(page.request, pid, noiseSheetId)).map((c) => c.name))
    .not.toContain('topic');

  const editedSheetId = await importCsv(
    page.request,
    pid,
    '04-manual-edit.csv',
    csvFor('manual edit source row zero', 'manual edit source row one'),
  );
  const editedColumns = await sheetColumns(page.request, pid, editedSheetId);
  const editedRows = await sheetData(page.request, pid, editedSheetId, 0, 1);
  await editCells(page.request, pid, [{
    rowId: editedRows.rows[0].id,
    columnId: editedColumns[0].id,
    value: 'manual edit applied in long-lived setup',
  }]);

  for (let i = 0; i < 3; i += 1) {
    await importCsv(
      page.request,
      pid,
      `0${5 + i}-visible-filler-${i}.csv`,
      csvFor(`visible filler ${i} row zero`, `visible filler ${i} row one`),
    );
  }

  const targetSheetId = await importCsv(
    page.request,
    pid,
    '08-current-target.csv',
    csvFor(
      `current target row zero ${TARGET_TRACE_SENTINEL}`,
      'current target row one',
    ),
  );
  // Replies for the classify form run below. The form ticks Include
  // confidence and Include justification, so the justification carries the
  // response sentinel the row drawer must show.
  primeMapRowsReplies(
    pid,
    classifyAction(targetSheetId, TARGET_PROMPT, ['target', 'other'], true),
    classifyReplies('target', TARGET_RESPONSE_SENTINEL, true),
  );

  const sheets = await listSheets(page.request, pid);
  expect(sheets.length).toBeGreaterThanOrEqual(8);
  const targetSheet = sheets.find((sheet) => sheet.id === targetSheetId);
  expect(targetSheet).toBeTruthy();
  const oldSheet = sheets.find((sheet) => sheet.id === oldSheetId);
  expect(oldSheet).toBeTruthy();
  expect((await history(page.request, pid)).some((step) => step.kind === 'edit')).toBe(true);
  expect((await history(page.request, pid)).some((step) => (
    step.id === Number(templateOpId) && ['undone', 'discarded'].includes(step.status)
  ))).toBe(true);

  const actionPosts: ActionRunPost[] = [];
  const requestUrls: string[] = [];
  const columnRunUrls: string[] = [];
  const traceUrls: string[] = [];
  page.on('request', (request) => {
    const url = request.url();
    requestUrls.push(url);
    if (
      request.method() === 'POST' &&
      url.includes(`/api/projects/${pid}/actions/v1/run`)
    ) {
      try {
        const post = actionPostFrom(request.postDataJSON());
        if (post) actionPosts.push(post);
      } catch {
        // The assertion below fails if the product path did not send JSON.
      }
    }
    if (url.includes(`/api/projects/${pid}/columns/`) && url.includes('/runs')) {
      columnRunUrls.push(url);
    }
    if (url.includes(`/api/projects/${pid}/actions/runs/`) && url.includes('/trace')) {
      traceUrls.push(url);
    }
  });

  await exposeReplayModel(page, pid);
  await openProject(page, pid, oldSheetId);
  await openAction(page, 'map.classify');
  const form = page.getByTestId('generated-action-form');
  await expect(form).toContainText(`on ${oldSheet!.name}`);

  // Switching to the newest of eight sheets, which only the More tabs menu
  // reaches, closes the form bound to the old sheet; the form opened there is
  // bound to the target sheet.
  await expect(page.getByTestId(`workbench-mainView-tab-${targetSheetId}`)).toHaveCount(0);
  await page.getByTestId('workbench-mainView-more-tabs').click();
  await page.getByRole('menuitem', { name: targetSheet!.name }).click();
  await expect(page.getByTestId(`workbench-mainView-tab-${targetSheetId}`))
    .toHaveAttribute('aria-selected', 'true');
  await expect(form).toHaveCount(0);
  await openAction(page, 'map.classify');
  await expect(form).toContainText(`on ${targetSheet!.name}`);
  await selectActionSourceColumns(page, 'snippet');
  const engineTrigger = page.getByTestId('field-engine').locator('.engine-selector__trigger');
  await engineTrigger.click();
  const engineDialog = page.getByTestId('engine-selector-dialog');
  await engineDialog.getByRole('searchbox', { name: 'Search Engine' }).fill('Gemini 3.5 Flash-Lite');
  await engineDialog.locator('[data-engine-selector-choice="replay-engine-model"]').click();
  await expect(engineTrigger).toContainText('Gemini 3.5 Flash-Lite');
  await page.getByTestId('field-context').fill(TARGET_PROMPT);
  await page.getByTestId('output-field-name').fill('topic');
  await page.getByLabel('Field 1 description').fill('Best-fit label from the list');
  await page.getByLabel('Field 1 labels').fill('target, other');
  await form.getByLabel('Include Confidence').check();
  await form.getByLabel('Include Justification').check();

  const runButton = page.getByTestId('generated-action-run');
  await expect(runButton).toBeEnabled({ timeout: 15_000 });
  const actionRunResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST' &&
    response.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
    response.status() !== 402
  ));
  await clickRunButton(page);
  const actionRunBody = await (await actionRunResponse).json();
  expect(actionRunBody.schema_version).toBe('frisket.action_result.v1');
  const targetRunId = Number(actionRunBody.run_id);
  expect(Number.isInteger(targetRunId)).toBe(true);

  await expect(page.getByTestId('run-progress')).toContainText('complete', { timeout: 60_000 });

  const targetPost = actionPosts.find((post) => (
    post.action_id === 'map.classify' &&
    post.params?.context === TARGET_PROMPT
  ));
  expect(targetPost).toBeTruthy();
  expect(targetPost!.scope).toEqual({ kind: 'sheet_rows', sheet_id: targetSheetId });
  expect(targetPost!.params?.source).toEqual(['snippet']);
  expect(targetPost!.params?.fields).toMatchObject([{ name: 'topic' }]);
  expect(requestUrls.some((url) => url.includes(`/api/projects/${pid}/run`))).toBe(false);

  // Dismiss the overlay action drawer (workbench-ia-action-drawer-v1) via its
  // own close button; it covers the far-right chrome, so a later header click
  // would otherwise be intercepted by the overlay.
  if (await page.getByTestId('action-drawer').isVisible().catch(() => false)) {
    await page.getByTestId('action-drawer').getByTestId('action-drawer-close').click();
    await expect(page.getByTestId('action-drawer')).toHaveCount(0);
  }

  // The older sheet's "topic" column, same name and same row index, is
  // untouched by the run on the target sheet.
  const oldColumnsAfter: WireColumnWithRun[] = await sheetColumns(page.request, pid, oldSheetId);
  const oldTopicAfter = oldColumnsAfter.find((column) => column.id === oldTopic!.id);
  expect(oldTopicAfter?.latest_run_id).toBe(oldTopic!.latest_run_id);
  expect(oldTopicAfter?.latest_run_id).not.toBe(targetRunId);
  const oldRows = await sheetData(page.request, pid, oldSheetId, 0, 2);
  expect(oldRows.rows.map((row) => row.cells[String(oldTopic!.id)])).toEqual(['old', 'old']);

  const targetColumns: WireColumnWithRun[] = await sheetColumns(page.request, pid, targetSheetId);
  const targetRows = await sheetData(page.request, pid, targetSheetId, 0, 1);
  const targetTopic = targetColumns.find((column) => column.name === 'topic');
  expect(targetTopic).toBeTruthy();
  expect(targetTopic!.latest_run_id).toBe(targetRunId);

  const columnRunsResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === 'GET' &&
      url.pathname.includes(`/api/projects/${pid}/columns/${targetTopic!.id}/runs`) &&
      url.searchParams.get('offset') === '0' &&
      url.searchParams.get('limit') === '20';
  });
  await clickHeader(page, targetColumns, 'topic');
  const columnRunsBody = await (await columnRunsResponse).json();
  expect(columnRunsBody.column.latest_run_id).toBe(targetRunId);
  expect(columnRunsBody.offset).toBe(0);
  expect(columnRunsBody.limit).toBe(20);
  expect(columnRunsBody.total).toBeGreaterThanOrEqual(1);
  expect(columnRunsBody.has_more).toBe(false);
  await expect(page.getByTestId('column-drawer')).toBeVisible();
  await expect(page.getByTestId('column-prompt')).toContainText(TARGET_TRACE_SENTINEL);
  await expect(page.getByTestId('column-prompt')).not.toContainText(OLD_TRACE_SENTINEL);
  await expect(page.getByTestId('column-prompt')).not.toContainText(OTHER_SHEET_SENTINEL);
  expect(columnRunUrls.some((url) => url.includes(`/columns/${targetTopic!.id}/runs`))).toBe(true);
  await page.getByLabel('Close drawer').click();
  await expect(page.getByTestId('column-drawer')).toHaveCount(0);

  await openCellDrawer(page, targetColumns, 'topic', 0);
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  const topicField = drawer.getByTestId('row-field-topic');
  await expect(topicField.getByTestId('prov-justification')).toContainText(TARGET_RESPONSE_SENTINEL);
  await expect(topicField).not.toContainText(OLD_TRACE_SENTINEL);
  await expect(topicField).not.toContainText(OTHER_SHEET_SENTINEL);

  const traceResponse = page.waitForResponse((response) => (
    response.request().method() === 'GET' &&
    response.url().includes(
      `/api/projects/${pid}/actions/runs/${targetRunId}/trace/rows/${targetRows.rows[0].id}`,
    ) &&
    response.url().includes(`column_id=${targetTopic!.id}`)
  ));
  await topicField.hover();
  await topicField.getByTestId('cell-action-explain').click();
  const traceBody = await (await traceResponse).json();
  expect(traceBody.recorded).toBe(true);
  expect(traceBody.action).toBe('run_trace_row');
  expect(traceBody.row_id).toBe(Number(targetRows.rows[0].id));
  expect(traceBody.column_id).toBe(Number(targetTopic!.id));
  expect(traceBody.trace).not.toHaveProperty('rows');
  const panel = topicField.getByTestId('explain-panel');
  await expect(panel).toBeVisible();
  await expect(panel).toContainText(TARGET_TRACE_SENTINEL);
  await expect(panel).not.toContainText(OLD_TRACE_SENTINEL);
  await expect(panel).not.toContainText(OTHER_SHEET_SENTINEL);
  expect(traceUrls.some((url) => (
    url.includes(`/actions/runs/${targetRunId}/trace/rows/${targetRows.rows[0].id}`)
  ))).toBe(true);

  const traceCountBeforeManualEdit = traceUrls.length;
  await editCells(page.request, pid, [{
    rowId: targetRows.rows[0].id,
    columnId: targetTopic!.id,
    value: 'manual override on target topic',
  }]);
  await page.getByLabel('Close drawer').click();
  await openProject(page, pid, targetSheetId);
  await openCellDrawer(page, targetColumns, 'topic', 0);
  const editedDrawer = page.getByTestId('row-drawer');
  const editedTopicField = editedDrawer.getByTestId('row-field-topic');
  await expect(editedTopicField).toContainText('manual override on target topic');
  await expect(editedTopicField.getByTestId('cell-provenance-origin')).toContainText(
    'manual edit',
  );
  await editedTopicField.hover();
  await expect(editedTopicField.getByTestId('cell-action-explain')).toHaveCount(0);
  expect(traceUrls.length).toBe(traceCountBeforeManualEdit);
});
