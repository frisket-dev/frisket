import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';

import { expect, test, type APIRequestContext } from '@playwright/test';

import {
  clickCell,
  clickHeader,
  clickRunButton,
  createProject,
  editCells,
  importCsv,
  listSheets,
  openAction,
  openProject,
  runAndWait,
  sheetColumns,
  sheetData,
  type WireColumn,
  uniqueName,
} from './helpers';

const MODEL = 'gemini/gemini-2.5-flash';
const TARGET_TRACE_SENTINEL = 'TARGET_TRACE_SENTINEL_v1_long_lived';
const TARGET_RESPONSE_SENTINEL = 'TARGET_RESPONSE_SENTINEL_v1_long_lived';
const OLD_TRACE_SENTINEL = 'OLD_TRACE_SENTINEL_v1_long_lived';
const OTHER_SHEET_SENTINEL = 'OTHER_SHEET_SENTINEL_v1_long_lived';

const TARGET_PROMPT = `Classify the current target sheet only. ${TARGET_TRACE_SENTINEL}`;
const OLD_PROMPT = `Classify the older sheet only. ${OLD_TRACE_SENTINEL}`;
const OTHER_PROMPT = `Classify the same row index on another sheet only. ${OTHER_SHEET_SENTINEL}`;

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();

type WireColumnWithRun = WireColumn & { current_run_id?: number | null };

type HistoryStep = {
  id: number;
  kind: string;
  status: string;
};

type ActionRunPost = {
  schema_version?: unknown;
  kind?: unknown;
  params?: Record<string, unknown>;
  idempotency_key?: unknown;
};

function csvFor(...snippets: string[]): string {
  return `snippet\n${snippets.map((snippet) => JSON.stringify(snippet)).join('\n')}\n`;
}

function primeClassifyCache({
  pid,
  sheetId,
  prompt,
  topic,
  justification,
  labels,
  // extract-form-polish-v1 item 6: a fresh classify FORM launch now defaults
  // both toggles OFF, but runDebugClassify's direct-API companion call still
  // runs with them explicitly true — callers pass whichever value the actual
  // request (form or direct API) will carry, so the cache's request_key
  // matches.
  includeExtras = true,
}: {
  pid: string;
  sheetId: number;
  prompt: string;
  topic: string;
  justification: string;
  labels: string[];
  includeExtras?: boolean;
}): void {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = `
import json
import sys
from pathlib import Path
from frisket.ai.llm import LLMRequest, LLMResponse, ResponseCache, request_key
from frisket.sdk.ops.classify import ClassifyRecipe
from frisket.engine.store import Project

workspace, pid, sheet_id_raw, prompt, topic, justification, labels_raw = sys.argv[1:]
sheet_id = int(sheet_id_raw)
labels = json.loads(labels_raw)
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    spec = {
        "recipe": "classify",
        "model": ${JSON.stringify(MODEL)},
        "sheet_id": sheet_id,
        "input_columns": ["snippet"],
        "context": prompt,
        "fields": [
            {
                "name": "topic",
                "type": "category",
                "description": "Best-fit label from the list",
                "labels": labels,
            }
        ],
        "include_justification": ${includeExtras ? 'True' : 'False'},
        "include_confidence": ${includeExtras ? 'True' : 'False'},
    }
    columns = {c["name"]: c["id"] for c in project.columns(sheet_id)}
    values = {
        name: project.get_values(sheet_id, columns[name])
        for name in spec["input_columns"]
    }
    cache = ResponseCache(project.path / "project.cache.db")
    recipe = ClassifyRecipe()
    try:
        for row_id in project.visible_row_ids(sheet_id):
            row_values = {name: values[name].get(row_id) for name in spec["input_columns"]}
            call = recipe.render(row_values, spec)
            req = LLMRequest(
                model=spec["model"],
                messages=call.messages,
                schema=call.schema,
                max_tokens=call.max_tokens,
            )
            data = {
                "topic": topic,
                "topic_confidence": 0.93,
                "topic_justification": justification,
            }
            cache.put(
                request_key(req, recipe.version),
                LLMResponse(
                    content=json.dumps(data),
                    data=data,
                    tokens_in=120,
                    tokens_out=30,
                    cost=0.0001,
                    model=spec["model"],
                ),
            )
    finally:
        cache.close()
finally:
    project.close()
`;
  execFileSync(
    'uv',
    [
      'run',
      'python',
      '-c',
      script,
      workspace,
      pid,
      String(sheetId),
      prompt,
      topic,
      justification,
      JSON.stringify(labels),
    ],
    {
      cwd: REPO_ROOT,
      stdio: 'pipe',
      timeout: 60_000,
    },
  );
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
  primeClassifyCache({ pid, sheetId, prompt, topic, justification, labels });
  return runAndWait(request, pid, {
    recipe: 'classify',
    sheet_id: sheetId,
    input_columns: ['snippet'],
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
    include_justification: true,
    include_confidence: true,
  });
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

function actionPostFrom(data: unknown): ActionRunPost | null {
  if (data === null || typeof data !== 'object' || Array.isArray(data)) return null;
  const record = data as Record<string, unknown>;
  const params = record.params;
  return {
    schema_version: record.schema_version,
    kind: record.kind,
    params: params !== null && typeof params === 'object' && !Array.isArray(params)
      ? params as Record<string, unknown>
      : undefined,
    idempotency_key: record.idempotency_key,
  };
}

test('v1 web paths keep stable sheet/run/row targets in a long-lived project', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-long-lived-v1'));

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
  const oldTopic = (await sheetColumns(page.request, pid, oldSheetId))
    .find((column) => column.name === 'topic');
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
  primeClassifyCache({
    pid,
    sheetId: targetSheetId,
    prompt: TARGET_PROMPT,
    topic: 'target',
    justification: TARGET_RESPONSE_SENTINEL,
    labels: ['target', 'other'],
    // This one feeds the live classify FORM below (openAction + Run), which
    // never touches the confidence/justification toggles — they default OFF.
    includeExtras: false,
  });

  const sheets = await listSheets(page.request, pid);
  expect(sheets.length).toBeGreaterThanOrEqual(8);
  const targetSheet = sheets.find((sheet) => sheet.id === targetSheetId);
  expect(targetSheet).toBeTruthy();
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

  await openProject(page, pid, oldSheetId);
  await openAction(page, 'map.classify');
  await expect(page.getByTestId('action-form')).toBeVisible();
  // A fresh launch never guesses a destination from the old civic sample,
  // even when this sheet already carries a "topic" column from an earlier run.
  await expect(page.getByTestId('new-column-name')).toHaveValue('');
  await expect(page.getByTestId('save-to-overwrite-warning')).toHaveCount(0);
  await expect(page.getByTestId('save-to-new-column-badge')).toHaveCount(0);
  await expect(page.getByTestId('default-column-name-collision-note')).toHaveCount(0);

  await page.getByTestId(`workbench-mainView-tab-${targetSheetId}`).click();
  await expect(page.getByTestId('action-form')).toContainText(`on ${targetSheet!.name}`);
  await expect(page.getByTestId('new-column-name')).toHaveValue('topic');
  await expect(page.getByTestId('classify-source-column-select')).toHaveValue('snippet');

  await page.getByTestId('action-prompt').fill(TARGET_PROMPT);
  await page.getByTestId('new-column-name').fill('topic');
  await page.getByLabel('Field 1 labels').fill('target, other');

  const runButton = page.getByTestId('run-button');
  await expect
    .poll(async () => {
      return (await runButton.isEnabled()) ? 'ready' : 'waiting';
    }, { timeout: 15_000 })
    .toBe('ready');
  const actionRunResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST' &&
    response.url().includes(`/api/projects/${pid}/actions/v1/run`)
  ));
  // This classify run's estimate lands above the local confirmation threshold,
  // so the drawer run flow shows the cost gate; confirm it (default) so the run
  // POSTs.
  await clickRunButton(page);
  const actionRunBody = await (await actionRunResponse).json();
  expect(actionRunBody.schema_version).toBe('frisket.action_result.v1');
  const targetRunId = Number(actionRunBody.run_id);
  expect(Number.isInteger(targetRunId)).toBe(true);

  await expect(page.getByTestId('run-progress')).toContainText('complete', { timeout: 60_000 });

  const targetPost = actionPosts.find((post) => (
    post.kind === 'map.classify' &&
    post.params?.context === TARGET_PROMPT
  ));
  expect(targetPost).toBeTruthy();
  expect(targetPost!.schema_version).toBe('frisket.action.v2');
  expect(targetPost!.params?.sheet_id).toBe(targetSheetId);
  expect(targetPost!.params?.input_columns).toEqual(['snippet']);
  expect(targetPost!.params?.fields).toMatchObject([{ name: 'topic' }]);
  expect(targetPost!.params?.row_ids).toBeUndefined();
  expect(String(targetPost!.idempotency_key)).toContain('map.classify');
  expect(requestUrls.some((url) => url.includes(`/api/projects/${pid}/run`))).toBe(false);

  // Dismiss the overlay action drawer (workbench-ia-action-drawer-v1) via its
  // own close button; it covers the far-right chrome, so a later header click
  // would otherwise be intercepted by the overlay.
  if (await page.getByTestId('action-drawer').isVisible().catch(() => false)) {
    await page.getByTestId('action-drawer').getByTestId('action-drawer-close').click();
    await expect(page.getByTestId('action-drawer')).toHaveCount(0);
  }

  const targetColumns = await sheetColumns(page.request, pid, targetSheetId);
  const targetRows = await sheetData(page.request, pid, targetSheetId, 0, 1);
  const targetTopic = targetColumns.find((column) => (
    column.name === 'topic'
  )) as WireColumnWithRun | undefined;
  expect(targetTopic).toBeTruthy();
  expect(targetTopic!.current_run_id).toBe(targetRunId);

  const columnRunsResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === 'GET' &&
      url.pathname.includes(`/api/projects/${pid}/columns/${targetTopic!.id}/runs`) &&
      url.searchParams.get('offset') === '0' &&
      url.searchParams.get('limit') === '20';
  });
  await clickHeader(page, targetColumns, 'topic');
  const columnRunsBody = await (await columnRunsResponse).json();
  expect(columnRunsBody.column.current_run_id).toBe(targetRunId);
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

  await clickCell(page, targetColumns, 'topic', 0);
  await page.keyboard.press('Enter');
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
  await clickCell(page, targetColumns, 'topic', 0);
  await page.keyboard.press('Enter');
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
