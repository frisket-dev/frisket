import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { expect, test, type Page } from '@playwright/test';

import {
  clickGroupHeader,
  clickHeaderMenu,
  clickRunButton,
  createProject,
  importCsv,
  openProject,
  runAndWait,
  sheetColumns,
  sheetData,
  uniqueName,
  type WireColumn,
} from './helpers';
import { prepareOwnModelReplay, REPLAY_MODEL as MODEL } from './replayModelScenario';

const ARTIFACT_DIR = '/home/dev/actor-artifacts/action-compare-20261025/connected';
const PROMPT_A = 'Classify each local news item by its primary subject.';
const PROMPT_B = 'Classify each local news item conservatively; use other unless the subject is explicit.';
const LABELS = ['transit', 'money', 'other'];
const CSV = `snippet
"The transit authority cut weekend bus service."
"A council member's spouse won a city contract."
"The county fair opened with record attendance."
`;
const REPO_ROOT = path.basename(process.cwd()) === 'web'
  ? path.resolve(process.cwd(), '..')
  : process.cwd();
const PYTHON = process.env.FRISKET_E2E_PYTHON
  ?? path.join(REPO_ROOT, '.venv/bin/python');

type RunColumn = WireColumn & { current_run_id?: number | null };

function classifyAction(sheetId: number, prompt: string, idempotencyKey: string) {
  return {
    action_id: 'map.classify',
    scope: { kind: 'sheet_rows' as const, sheet_id: sheetId },
    params: {
      source: ['snippet'],
      engine: 'llm',
      model: MODEL,
      context: prompt,
      fields: [{ name: 'topic', type: 'category', labels: LABELS }],
      include_justification: false,
      include_confidence: false,
    },
    output_names: { topic: 'topic' },
    idempotency_key: idempotencyKey,
  };
}

/** Prime the exact typed map plan used by both a persisted run and a scratch
 * comparison preview. replay_strict then proves every model-shaped call is a
 * cache hit while the HTTP/action/worker path remains the real application. */
function primeClassifyCache(
  pid: string,
  sheetId: number,
  prompt: string,
  topics: string[],
): void {
  const workspace = process.env.FRISKET_E2E_WS
    ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = `
import json
import sys
from pathlib import Path
from frisket.actions.system import typed_action_for_request
from frisket.ai.llm import LLMRequest, LLMResponse, ResponseCache, request_key
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.store import Project

workspace, pid, sheet_id_raw, model, prompt, labels_raw, topics_raw = sys.argv[1:]
sheet_id = int(sheet_id_raw)
labels = json.loads(labels_raw)
topics = json.loads(topics_raw)
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    action = {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["snippet"],
            "engine": "llm",
            "model": model,
            "context": prompt,
            "fields": [{"name": "topic", "type": "category", "labels": labels}],
            "include_justification": False,
            "include_confidence": False,
        },
        "output_names": {"topic": "topic"},
        "idempotency_key": "action-compare-connected-cache-prime",
    }
    plan = build_typed_map_rows_plan(project, typed_action_for_request(action))
    spec = plan.spec_dict()
    columns = {column["name"]: column["id"] for column in project.columns(sheet_id)}
    source_values = project.get_values(sheet_id, columns["snippet"])
    row_ids = project.visible_row_ids(sheet_id)
    if len(row_ids) != len(topics):
        raise RuntimeError(f"expected {len(row_ids)} topics, received {len(topics)}")
    cache = ResponseCache(project.path / "project.cache.db")
    try:
        for row_id, topic in zip(row_ids, topics, strict=True):
            call = plan.program.render({"snippet": source_values.get(row_id)}, spec)
            request = LLMRequest(
                model=spec["model"],
                messages=call.messages,
                schema=call.schema,
                max_tokens=call.max_tokens,
            )
            data = {"topic": topic}
            cache.put(
                request_key(request, plan.program.version),
                LLMResponse(
                    content=json.dumps(data),
                    data=data,
                    tokens_in=24,
                    tokens_out=4,
                    cost=0.0,
                    model=spec["model"],
                ),
            )
    finally:
        cache.close()
finally:
    project.close()
`;
  execFileSync(PYTHON, [
    '-c', script, workspace, pid, String(sheetId), MODEL, prompt,
    JSON.stringify(LABELS), JSON.stringify(topics),
  ], {
    cwd: REPO_ROOT,
    stdio: 'pipe',
    timeout: 60_000,
    env: { ...process.env, PYTHONPATH: path.join(REPO_ROOT, 'src') },
  });
}

async function shoot(page: Page, name: string): Promise<void> {
  mkdirSync(ARTIFACT_DIR, { recursive: true });
  await page.screenshot({
    path: path.join(ARTIFACT_DIR, name),
    animations: 'disabled',
    fullPage: true,
  });
}

async function approveCostGateIfShown(page: Page): Promise<void> {
  const gate = page.getByTestId('cost-gate-modal');
  if (!await gate.isVisible({ timeout: 1_000 }).catch(() => false)) return;
  await page.getByTestId('cost-gate-input').fill('confirm');
  await page.getByTestId('cost-gate-confirm').click();
}

async function topicValues(
  page: Page,
  pid: string,
  sheetId: number,
  topicColumnId: number,
  count: number,
): Promise<unknown[]> {
  const data = await sheetData(page.request, pid, sheetId, 0, count);
  return data.rows.map((row) => row.cells[String(topicColumnId)]);
}

async function chooseCompareFromColumnMenu(
  page: Page,
  columns: WireColumn[],
  columnName: string,
): Promise<void> {
  await clickHeaderMenu(page, columns, columnName);
  await expect(page.getByTestId('header-menu-action-run-section')).toBeVisible();
  await page.getByTestId('header-menu-compare-action-run').click();
  await expect(page.getByTestId('action-compare-toolbar')).toBeVisible({ timeout: 20_000 });
}

async function selectEveryRow(page: Page): Promise<void> {
  const grid = page.getByTestId('grid');
  const box = await grid.boundingBox();
  if (!box) throw new Error('grid is not visible');
  const groupHeight = Number(await grid.getAttribute('data-group-header-height') ?? 0);
  // Glide's top-left row-marker checkbox selects the full current row set.
  await page.mouse.click(box.x + 20, box.y + groupHeight + 17);
}

test.describe('connected action comparison', () => {
  test('compares a real run, applies replacement settings, and undoes them', async ({ page }) => {
    test.setTimeout(120_000);
    const pid = await createProject(page.request, uniqueName('e2e-action-compare-connected'));
    await prepareOwnModelReplay(page.request, pid);
    const sheetId = await importCsv(page.request, pid, 'local-news.csv', CSV);
    const baseline = ['transit', 'money', 'other'];
    const challenger = ['other', 'other', 'other'];
    primeClassifyCache(pid, sheetId, PROMPT_A, baseline);
    primeClassifyCache(pid, sheetId, PROMPT_B, challenger);

    const baselineRunId = await runAndWait(
      page.request,
      pid,
      classifyAction(sheetId, PROMPT_A, `action-compare-baseline:${pid}`),
    );
    await openProject(page, pid, sheetId);
    await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
    const columns = await sheetColumns(page.request, pid, sheetId);
    const topic = columns.find((column) => column.name === 'topic') as RunColumn | undefined;
    expect(topic).toBeTruthy();
    expect(topic!.current_run_id).toBe(baselineRunId);
    expect(await topicValues(page, pid, sheetId, topic!.id, 3)).toEqual(baseline);

    await clickGroupHeader(page, columns, 'topic', 'topic');
    const entry = page.getByTestId('action-run-entry-card');
    await expect(entry).toBeVisible();
    await expect(entry).toContainText('Classify');
    await expect(entry).toContainText('snippet');
    await expect(entry).toContainText('topic');
    await expect(entry.getByTestId('action-run-entry-compare')).toBeVisible();
    await shoot(page, '01-run-entry.png');

    // Toggle the pinned group card closed, then prove the same family actions
    // are available from the individual generated-column menu.
    await clickGroupHeader(page, columns, 'topic', 'topic');
    await expect(entry).toHaveCount(0);
    await clickHeaderMenu(page, columns, 'topic');
    await expect(page.getByTestId('header-menu-action-run-section')).toContainText('Classify');
    await expect(page.getByTestId('header-menu-compare-action-run')).toBeVisible();
    await expect(page.getByTestId('header-menu-edit-action-run')).toBeVisible();
    await expect(page.getByTestId('header-menu-earlier-action-runs')).toBeVisible();
    await shoot(page, '02-column-menu.png');

    await page.getByTestId('header-menu-earlier-action-runs').click();
    const history = page.getByTestId('action-compare-history-dialog');
    await expect(history).toBeVisible();
    await expect(history).toContainText('1 stored run');
    await expect(history.getByTestId(`action-compare-history-choose-${baselineRunId}`)).toBeVisible();
    await shoot(page, '03-earlier-runs.png');
    await page.getByLabel('Close earlier runs').click();

    await chooseCompareFromColumnMenu(page, columns, 'topic');
    await expect(page.getByTestId('action-compare-scratch-tag')).toHaveText('Comparing 3 sample rows');
    await expect(page.getByTestId('action-compare-variant-A')).toContainText('in table');
    await shoot(page, '04-current-values.png');

    await page.getByRole('button', { name: 'Variant', exact: true }).click();
    const editor = page.getByRole('complementary', { name: 'Configure variant B' });
    await expect(editor).toBeVisible();
    await editor.getByTestId('field-context').fill(PROMPT_B);
    await shoot(page, '05-variant-editor.png');
    await editor.getByRole('button', { name: 'Close variant editor' }).click();

    await page.getByRole('button', { name: 'Re-run', exact: true }).click();
    await approveCostGateIfShown(page);
    const variantB = page.getByTestId('action-compare-variant-B');
    await expect(variantB).toContainText('ready', { timeout: 60_000 });
    await expect(page.locator("td[data-comparison='different']")).toHaveCount(2);
    await shoot(page, '06-preview-results.png');

    await page.getByRole('button', { name: 'Use settings from variant B' }).click();
    const replacementForm = page.getByTestId('generated-action-form');
    await expect(replacementForm).toBeVisible();
    await expect(replacementForm.getByTestId('replacement-overlay-notice')).toBeVisible();
    await expect(replacementForm.getByTestId('field-context')).toHaveValue(PROMPT_B);
    await shoot(page, '07-replace-settings.png');

    await clickRunButton(page);
    await expect(page.getByTestId('run-progress')).toContainText('complete', { timeout: 60_000 });
    await expect.poll(
      () => topicValues(page, pid, sheetId, topic!.id, 3),
      { timeout: 20_000 },
    ).toEqual(challenger);
    await shoot(page, '08-replaced-grid.png');

    await expect(page.getByTestId('undo-button')).toBeEnabled();
    await page.getByTestId('undo-button').click();
    await expect.poll(
      () => topicValues(page, pid, sheetId, topic!.id, 3),
      { timeout: 20_000 },
    ).toEqual(baseline);
    await shoot(page, '09-undone-grid.png');
  });

  test('samples 20 default rows but preserves a larger captured selection', async ({ page }) => {
    test.setTimeout(120_000);
    const pid = await createProject(page.request, uniqueName('e2e-action-compare-selection'));
    await prepareOwnModelReplay(page.request, pid);
    const csv = `snippet\n${Array.from(
      { length: 25 },
      (_, index) => `"selection row ${index + 1}"`,
    ).join('\n')}\n`;
    const sheetId = await importCsv(page.request, pid, 'selection.csv', csv);
    primeClassifyCache(pid, sheetId, PROMPT_A, Array(25).fill('other'));
    await runAndWait(
      page.request,
      pid,
      classifyAction(sheetId, PROMPT_A, `action-compare-selection:${pid}`),
    );
    await openProject(page, pid, sheetId);
    const columns = await sheetColumns(page.request, pid, sheetId);

    await chooseCompareFromColumnMenu(page, columns, 'topic');
    await expect(page.getByTestId('action-compare-scratch-tag')).toHaveText('Comparing 20 sample rows');
    await shoot(page, '10-default-sample.png');
    await page.getByTestId('action-compare-maintab-close').click();
    await expect(page.getByTestId('action-compare-toolbar')).toHaveCount(0);

    await selectEveryRow(page);
    await expect.poll(async () => page.evaluate(() => (
      window as typeof window & { __frisketGridSelection?: { rows: number[] } }
    ).__frisketGridSelection?.rows.length ?? 0)).toBe(25);
    await chooseCompareFromColumnMenu(page, columns, 'topic');
    await expect(page.getByTestId('action-compare-scratch-tag'))
      .toHaveText('Comparing 20 of 25 selected rows');
    await shoot(page, '11-captured-selection.png');
  });
});
