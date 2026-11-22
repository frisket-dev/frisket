// Golden regression for the hosted RSS/Joe Rogan derive case:
// AI-extracted string lists materialized as a People child sheet must keep
// AI display semantics, so literal model escapes render as human text.

import { expect, test, type APIRequestContext } from '@playwright/test';
import {
  clickCell,
  createProject,
  importCsv,
  listSheets,
  openPalette,
  openProject,
  primeMapRowsReplies,
  sheetData,
  typedActionRequest,
  uniqueName,
} from './helpers';
import {
  prepareOwnModelReplay,
  REPLAY_MODEL as MODEL,
} from './replayModelScenario';

const PROMPT = 'Extract every person mentioned as a short descriptive string.';
const SOURCE_SUMMARY =
  'Devon Larratt is a veteran of the Canadian Armed Forces and a professional arm wrestler who is widely considered one of the sport’s greatest competitors.\n\nLearn more about your ad choices.';
const ESCAPED_PERSON =
  'Devon Larratt \\u2014 veteran of the Canadian Armed Forces, professional arm wrestler, and one of the sport\\u2019s greatest competitors\\nHost of the conversation';

type V1ActionOutput = {
  kind?: string;
  name?: string;
  sheet_id?: number;
  column_id?: number;
  ref?: Record<string, unknown>;
};

type V1ActionResult = {
  status?: string;
  run_id?: number | null;
  job_id?: number | null;
  outputs?: V1ActionOutput[];
};

type V1RunStatus = {
  run?: {
    status?: string | null;
    public_status?: {
      status?: string | null;
      live?: boolean;
      error?: string | null;
    };
  };
};

function csvEscape(value: string): string {
  return `"${value.replace(/"/g, '""')}"`;
}

function primePeopleCache(pid: string, sheetId: number): void {
  const action = typedActionRequest('map.extract', {
    sheetId,
    params: {
      source: ['episode', 'summary'],
      model: MODEL,
      instruction: PROMPT,
      fields: [
        {
          name: 'people',
          type: 'list',
          description: 'People mentioned',
          items: { type: 'string' },
        },
      ],
      include_confidence: false,
      grounding: { enabled: false },
    },
    outputNames: { people: 'people' },
  });
  primeMapRowsReplies(pid, action, [{ people: [ESCAPED_PERSON] }]);
}

function sourceRefForDerive(output: V1ActionOutput): Record<string, unknown> {
  const ref = output.ref ?? {};
  const sheetId = typeof ref.sheet_id === 'number' ? ref.sheet_id : output.sheet_id;
  const columnId = typeof ref.column_id === 'number' ? ref.column_id : output.column_id;
  const runId = typeof ref.run_id === 'number' ? ref.run_id : null;
  const route = typeof ref.route === 'string' ? ref.route : output.name;
  const schema = typeof ref.schema === 'string' ? ref.schema : `${route}_list`;
  if (
    typeof sheetId !== 'number' ||
    typeof columnId !== 'number' ||
    typeof runId !== 'number' ||
    typeof route !== 'string' ||
    typeof schema !== 'string'
  ) {
    throw new Error('map.extract did not return a valid derive source ref');
  }
  return {
    kind: 'named_result',
    sheet_id: sheetId,
    column_id: columnId,
    run_id: runId,
    route,
    schema,
  };
}

function sheetIdFromOutput(output: V1ActionOutput): number {
  const ref = output.ref ?? {};
  const sheetId = typeof output.sheet_id === 'number' ? output.sheet_id : ref.sheet_id;
  if (typeof sheetId !== 'number') {
    throw new Error('derive.table_from_list did not return a sheet id');
  }
  return sheetId;
}

async function waitForV1Run(
  request: APIRequestContext,
  pid: string,
  runId: number,
): Promise<void> {
  for (let i = 0; i < 300; i++) {
    const response = await request.get(`/api/projects/${pid}/actions/runs/${runId}/status`);
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as V1RunStatus;
    const run = body.run ?? {};
    const publicStatus = run.public_status ?? {};
    const status = run.status ?? publicStatus.status ?? null;
    const live = publicStatus.live === true;
    const error = typeof publicStatus.error === 'string' ? publicStatus.error : null;
    if (['failed', 'cancelled', 'stalled', 'orphaned'].includes(status ?? '')) {
      throw new Error(`run ${runId} ${status}${error ? `: ${error}` : ''}`);
    }
    if (status !== 'queued' && status !== 'running' && !live) return;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`run ${runId} did not finish`);
}

async function runV1ActionAndWait(
  request: APIRequestContext,
  pid: string,
  action: Record<string, unknown>,
): Promise<V1ActionResult> {
  const response = await request.post(`/api/projects/${pid}/actions/v1/run`, { data: action });
  expect(response.ok()).toBeTruthy();
  const body = (await response.json()) as V1ActionResult;
  if (body.status === 'queued' || body.status === 'running') {
    if (typeof body.run_id !== 'number') {
      throw new Error('queued v1 action did not return a run id');
    }
    await waitForV1Run(request, pid, body.run_id);
    const replay = await request.post(`/api/projects/${pid}/actions/v1/run`, { data: action });
    expect(replay.ok()).toBeTruthy();
    return (await replay.json()) as V1ActionResult;
  }
  return body;
}

async function runPeopleDeriveComposite(
  request: APIRequestContext,
  pid: string,
  sheetId: number,
): Promise<number> {
  const itemSchema = { type: 'string' };
  const extractBody = await runV1ActionAndWait(
    request,
    pid,
    {
      action_id: 'map.extract',
      scope: { kind: 'sheet_rows', sheet_id: sheetId },
      params: {
        source: ['episode', 'summary'],
        model: MODEL,
        instruction: PROMPT,
        fields: [
          {
            name: 'people',
            type: 'list',
            description: 'People mentioned',
            items: itemSchema,
          },
        ],
        include_confidence: false,
        grounding: { enabled: false },
      },
      output_names: { people: 'people' },
      idempotency_key: `golden-people-extract@sha256:${pid}`,
    },
  );
  expect(extractBody.status).toBe('completed');
  const namedResult = extractBody.outputs?.find(
    (output) => output.kind === 'named_result' && output.name === 'people',
  );
  expect(namedResult).toBeTruthy();

  const deriveBody = await runV1ActionAndWait(
    request,
    pid,
    {
      action_id: 'derive.table_from_list',
      scope: { kind: 'project' },
      sheet_name: 'People',
      params: {
        source: sourceRefForDerive(namedResult as V1ActionOutput),
        item_schema: itemSchema,
        columns: [{ name: 'value', path: '$', type: 'text' }],
      },
      idempotency_key: `golden-people-derive@sha256:${pid}`,
    },
  );
  expect(deriveBody.status).toBe('completed');
  const sheetOutput = deriveBody.outputs?.find((output) => output.kind === 'sheet');
  expect(sheetOutput).toBeTruthy();
  return sheetIdFromOutput(sheetOutput as V1ActionOutput);
}

test('derived People values render decoded text and stay AI-marked through search', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-golden-people-search'));
  await prepareOwnModelReplay(page.request, pid);
  const csv =
    'episode,summary\n' +
    `${csvEscape('JRE #2322 - Devon Larratt')},${csvEscape(SOURCE_SUMMARY)}\n`;
  const sheetId = await importCsv(page.request, pid, 'joe-rogan-rss.csv', csv);
  primePeopleCache(pid, sheetId);

  const childSheetId = await runPeopleDeriveComposite(page.request, pid, sheetId);

  const sheets = await listSheets(page.request, pid);
  const child = sheets.find((s) => s.id === childSheetId);
  expect(child).toMatchObject({ name: 'People', parent_sheet_id: sheetId, rows: 1 });

  const childData = await sheetData(page.request, pid, childSheetId);
  const valueColumn = childData.columns.find((c) => c.name === 'value');
  expect(valueColumn).toBeTruthy();
  expect(valueColumn?.generated).toBe(true);
  expect(childData.rows[0].cells[String(valueColumn?.id)]).toBe(ESCAPED_PERSON);

  await openProject(page, pid, childSheetId);
  await expect(page.getByTestId('sheet-breadcrumb')).toContainText('via derive');
  await clickCell(page, childData.columns, 'value', 0);
  await page.keyboard.press('Enter');
  const valueField = page.getByTestId('row-field-value');
  await expect(valueField).toHaveAttribute('aria-label', 'value cell');
  const renderedValue = await valueField.locator('.row-field-value').textContent();
  expect(renderedValue).toContain('Devon Larratt — veteran');
  expect(renderedValue).toContain('sport’s greatest competitors');
  expect(renderedValue).toContain('\nHost of the conversation');
  expect(renderedValue).not.toContain('\\u2014');
  expect(renderedValue).not.toContain('\\u2019');
  expect(renderedValue).not.toContain('\\n');

  await openProject(page, pid, sheetId);
  await openPalette(page);
  await page.getByTestId('command-palette-input').fill('Devon');
  const peopleGroup = page
    .locator('.search-group')
    .filter({ has: page.locator('.search-group-label', { hasText: 'People' }) });
  await expect(peopleGroup).toBeVisible();
  const peopleHit = peopleGroup.getByTestId('search-hit').first();
  const hitText = await peopleHit.textContent();
  expect(hitText).toContain('Devon');
  expect(hitText).toContain('— veteran');
  expect(hitText).not.toContain('\\u2014');
  await peopleHit.click();

  await expect(page.getByTestId(`workbench-mainView-tab-${childSheetId}`)).toHaveClass(/active/);
  await expect(page.locator('.sheet-title')).toHaveText('People');
});
