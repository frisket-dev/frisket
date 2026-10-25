import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import {
  clickGroupHeader,
  createProject,
  importCsv,
  openAction,
  openProject,
  runAndWait,
  sheetColumns,
  uniqueName,
} from './helpers';
import {
  exposeReplayModel,
  prepareOwnModelReplay,
  REPLAY_MODEL as MODEL,
} from './replayModelScenario';

test.use({ deviceScaleFactor: 2 });

const REPO_ROOT = path.basename(process.cwd()) === 'web'
  ? path.resolve(process.cwd(), '..')
  : process.cwd();
const PYTHON = process.env.FRISKET_E2E_PYTHON ?? path.join(REPO_ROOT, '.venv/bin/python');
const ARTIFACT_DIR = path.join(
  process.env.FRISKET_COMPARE_SCREENSHOTS
    ?? path.resolve('screenshots/action-compare-connected'),
  'extract',
);
const CSV = `case_text
"Case 24-CV-0187: initial hearing 2026-11-04; a later public order reset it to 2026-11-18."
"Case 25-CR-0042: initial hearing 2026-12-02; the court later continued it to 2026-12-09."
"Case 23-FM-0311: initial hearing 2026-10-30; a later notice reset it to 2026-11-13."
`;

type ExtractField = { name: string; type: 'text' | 'date'; description: string };
type ExtractRow = { case_number: string; hearing_date: string };
type ExtractSettings = { context: string; fields: ExtractField[]; rows: ExtractRow[] };

const A: ExtractSettings = {
  context: 'Public court notices. Use the first hearing date stated in each notice.',
  fields: [
    { name: 'case_number', type: 'text', description: 'Public docket case number exactly as written.' },
    { name: 'hearing_date', type: 'date', description: 'First hearing date stated in the notice.' },
  ],
  rows: [
    { case_number: '24-CV-0187', hearing_date: '2026-11-04' },
    { case_number: '25-CR-0042', hearing_date: '2026-12-02' },
    { case_number: '23-FM-0311', hearing_date: '2026-10-30' },
  ],
};
const B: ExtractSettings = {
  context: 'Public court notices. Prefer the latest operative scheduling order.',
  fields: [
    { name: 'case_number', type: 'text', description: 'Normalized public docket identifier.' },
    { name: 'hearing_date', type: 'date', description: 'Most recently ordered hearing date.' },
  ],
  rows: [
    { case_number: '24-CV-0187', hearing_date: '2026-11-18' },
    { case_number: '25-CR-0042', hearing_date: '2026-12-09' },
    { case_number: '23-FM-0311', hearing_date: '2026-11-13' },
  ],
};
const C: ExtractSettings = {
  context: 'Public court notices. Report the current hearing date and retain the court case prefix.',
  fields: [
    { name: 'case_number', type: 'text', description: 'Case identifier with its civil, criminal, or family prefix.' },
    { name: 'hearing_date', type: 'date', description: 'Current scheduled date after continuances or resets.' },
  ],
  rows: [
    { case_number: 'CV 24-CV-0187', hearing_date: '2026-11-18' },
    { case_number: 'CR 25-CR-0042', hearing_date: '2026-12-09' },
    { case_number: 'FM 23-FM-0311', hearing_date: '2026-11-13' },
  ],
};

function extractAction(sheetId: number, settings: ExtractSettings, key: string) {
  return {
    action_id: 'map.extract',
    scope: { kind: 'sheet_rows' as const, sheet_id: sheetId },
    params: {
      source: ['case_text'], engine: 'llm', model: MODEL,
      instruction: 'Extract the requested public docket facts.',
      context: settings.context, fields: settings.fields,
      include_confidence: false, grounding: { enabled: false },
    },
    output_names: { case_number: 'case_number', hearing_date: 'hearing_date' },
    idempotency_key: key,
  };
}

function primeExtractCache(pid: string, sheetId: number, settings: ExtractSettings): void {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = `
import json, sys
from pathlib import Path
from frisket.actions.system import typed_action_for_request
from frisket.ai.llm import LLMRequest, LLMResponse, ResponseCache, request_key
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.store import Project

workspace, pid, sheet_raw, model, settings_raw = sys.argv[1:]
sheet_id, settings = int(sheet_raw), json.loads(settings_raw)
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    action = {
        "action_id": "map.extract",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["case_text"], "engine": "llm", "model": model,
            "instruction": "Extract the requested public docket facts.",
            "context": settings["context"], "fields": settings["fields"],
            "include_confidence": False, "grounding": {"enabled": False},
        },
        "output_names": {"case_number": "case_number", "hearing_date": "hearing_date"},
        "idempotency_key": "action-compare-extract-cache-prime",
    }
    plan = build_typed_map_rows_plan(project, typed_action_for_request(action))
    spec = plan.spec_dict()
    columns = {column["name"]: column["id"] for column in project.columns(sheet_id)}
    values = project.get_values(sheet_id, columns["case_text"])
    row_ids = project.visible_row_ids(sheet_id)
    if len(row_ids) != len(settings["rows"]):
        raise RuntimeError("fixture row count does not match the imported sheet")
    cache = ResponseCache(project.path / "project.cache.db")
    try:
        for row_id, data in zip(row_ids, settings["rows"], strict=True):
            call = plan.program.render({"case_text": values.get(row_id)}, spec)
            request = LLMRequest(model=spec["model"], messages=call.messages,
                                 schema=call.schema, max_tokens=call.max_tokens)
            cache.put(request_key(request, plan.program.version), LLMResponse(
                content=json.dumps(data), data=data, tokens_in=52, tokens_out=12,
                cost=0.0, model=spec["model"],
            ))
    finally:
        cache.close()
finally:
    project.close()
`;
  execFileSync(PYTHON, [
    '-c', script, workspace, pid, String(sheetId), MODEL, JSON.stringify(settings),
  ], {
    cwd: REPO_ROOT, stdio: 'pipe', timeout: 60_000,
    env: { ...process.env, PYTHONPATH: path.join(REPO_ROOT, 'src') },
  });
}

async function shoot(page: Page, name: string): Promise<void> {
  mkdirSync(ARTIFACT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(ARTIFACT_DIR, name), animations: 'disabled', fullPage: true });
}

async function approveCostGateIfShown(page: Page): Promise<void> {
  if (!await page.getByTestId('cost-gate-modal').isVisible({ timeout: 1_000 }).catch(() => false)) return;
  await page.getByTestId('cost-gate-input').fill('confirm');
  await page.getByTestId('cost-gate-confirm').click();
}

async function historyTotal(request: APIRequestContext, pid: string): Promise<number> {
  const response = await request.get(`/api/projects/${pid}/history?limit=1`);
  expect(response.ok()).toBeTruthy();
  return ((await response.json()) as { total: number }).total;
}

async function configureVariant(page: Page, key: 'B' | 'C', settings: ExtractSettings) {
  await page.getByRole('button', { name: 'Variant', exact: true }).click();
  const editor = page.getByRole('complementary', { name: `Configure variant ${key}` });
  await expect(editor).toBeVisible();
  await expect(editor.getByTestId('engine-selector')).toContainText('Gemini 3.5 Flash-Lite');
  if (key === 'B') await shoot(page, '02a-extract-editor-model.png');
  await expect(editor.getByTestId('output-structure-lock-hint')).toContainText(
    'Names, types, labels, and field structure stay the same; descriptions can be edited.',
  );
  const rows = editor.getByTestId('output-field-row');
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0).getByTestId('output-field-name')).toBeDisabled();
  await expect(rows.nth(0).getByTestId('output-field-name')).toHaveValue('case_number');
  await expect(rows.nth(0).getByTestId('output-field-type')).toBeDisabled();
  await expect(rows.nth(0).getByTestId('output-field-type')).toHaveValue('text');
  await expect(rows.nth(1).getByTestId('output-field-name')).toBeDisabled();
  await expect(rows.nth(1).getByTestId('output-field-type')).toHaveValue('date');
  await rows.nth(0).getByTestId('output-field-description').fill(settings.fields[0].description);
  await rows.nth(1).getByTestId('output-field-description').fill(settings.fields[1].description);
  await editor.locator('details.action-advanced > summary').click();
  await editor.getByTestId('field-context').fill(settings.context);
  await expect(page.getByTestId('action-compare-toolbar')).toBeInViewport();
  await expect(editor.getByRole('button', { name: 'Close variant editor' })).toBeInViewport();
  return editor;
}

test('Extract compares two typed fields across A/B/C and opens Use settings', async ({ page }) => {
  test.setTimeout(120_000);
  const pid = await createProject(page.request, uniqueName('e2e-action-compare-extract'));
  await prepareOwnModelReplay(page.request, pid);
  await exposeReplayModel(page, pid);
  const sheetId = await importCsv(page.request, pid, 'public-court-notices.csv', CSV);
  for (const settings of [A, B, C]) primeExtractCache(pid, sheetId, settings);
  await runAndWait(page.request, pid, extractAction(sheetId, A, `extract-baseline:${pid}`));

  await openProject(page, pid, sheetId);
  const columns = await sheetColumns(page.request, pid, sheetId);
  await clickGroupHeader(page, columns, 'case_number', 'hearing_date');
  const provenance = page.getByTestId('action-run-entry-card');
  await expect(provenance).toBeVisible();
  await expect(provenance).toContainText('Extract structured fields');
  await expect(provenance).toContainText('case_number');
  await expect(provenance).toContainText('hearing_date');
  await expect(provenance.getByTestId('action-run-entry-compare')).toBeVisible();
  await shoot(page, '01-extract-run.png');
  await provenance.getByTestId('action-run-entry-compare').click();

  await expect(page.getByTestId('action-compare-toolbar')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('action-compare-variant-A')).toContainText('in table');
  const editorB = await configureVariant(page, 'B', B);
  await shoot(page, '02-extract-variant-b-editor.png');
  await editorB.getByRole('button', { name: 'Close variant editor' }).click();

  const editorC = await configureVariant(page, 'C', C);
  await shoot(page, '03-extract-variant-c-editor.png');
  await editorC.getByRole('button', { name: 'Close variant editor' }).click();
  await expect(page.getByTestId('action-compare-variant-chips').locator('.action-compare-variant'))
    .toHaveCount(3);
  await shoot(page, '04-extract-abc-pending.png');

  await page.getByRole('button', { name: 'Re-run', exact: true }).click();
  await approveCostGateIfShown(page);
  await expect(page.getByTestId('action-compare-variant-B')).toContainText('ready', { timeout: 60_000 });
  await expect(page.getByTestId('action-compare-variant-C')).toContainText('ready', { timeout: 60_000 });
  await expect(page.getByRole('columnheader', { name: 'case_number' })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: 'hearing_date' })).toBeVisible();
  expect(await page.locator("td[data-comparison='different']").count()).toBeGreaterThan(0);
  await shoot(page, '05-extract-abc-results.png');
  await page.getByTestId('action-compare-variant-C').hover();
  await expect(page.getByRole('tooltip')).toContainText('Description');
  await shoot(page, '05b-setting-changes.png');

  await page.getByRole('button', { name: 'Use settings from variant C' }).click();
  const useForm = page.getByTestId('generated-action-form');
  await expect(useForm).toBeVisible();
  await expect(useForm.getByTestId('replacement-overlay-notice')).toBeVisible();
  await useForm.locator('details.action-advanced > summary').click();
  await expect(useForm.getByTestId('field-context')).toHaveValue(C.context);
  await expect(useForm.getByTestId('output-field-description').first())
    .toHaveValue(C.fields[0].description);
  await shoot(page, '06-extract-use-settings.png');
  await useForm.getByRole('button', { name: 'Choose run scope' }).click();
  await page.getByRole('menuitem', { name: 'Return to Compare' }).click();
  await expect(useForm).toHaveCount(0);
  await page.getByTestId('action-compare-maintab-close').click();
  await expect(page.getByTestId('action-compare-discard-warning')).toBeVisible();
  await shoot(page, '07-close-comparison.png');
  await page.getByTestId('action-compare-discard-confirm').click();
  await expect(page.getByTestId('action-compare-toolbar')).toHaveCount(0);
});

test('fresh Ask can enter Compare before any action executes', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-action-compare-fresh-ask'));
  await prepareOwnModelReplay(page.request, pid);
  await exposeReplayModel(page, pid);
  const sheetId = await importCsv(page.request, pid, 'public-court-questions.csv', CSV);
  const beforeHistory = await historyTotal(page.request, pid);

  await openProject(page, pid, sheetId);
  await openAction(page, 'map.ask');
  const form = page.getByTestId('generated-action-form');
  await expect(form.getByTestId('text-source-columns')
    .getByRole('button', { name: 'Remove case_text' })).toBeVisible();
  await form.getByLabel('Question', { exact: true }).fill('What is the current scheduled hearing date?');
  const trigger = form.getByTestId('engine-selector').locator('.engine-selector__trigger');
  await trigger.click();
  await page.getByTestId('engine-selector-dialog')
    .locator('[data-engine-selector-choice="replay-model"]').click();
  await expect(trigger).toContainText('Gemini 3.5 Flash-Lite');
  await form.getByTestId('field-output-answer').fill('current_hearing');
  await form.getByTestId('generated-action-run-scope-menu-button').click();
  await expect(form.getByTestId('generated-action-menu-compare')).toBeVisible();
  await shoot(page, '07-fresh-ask-compare-entry.png');
  await form.getByTestId('generated-action-menu-compare').click();

  await expect(page.getByTestId('action-compare-toolbar')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('action-compare-variant-A')).toContainText('pending');
  await expect(page.getByTestId('action-compare-scratch-tag')).toHaveText('Comparing 3 sample rows');
  expect((await sheetColumns(page.request, pid, sheetId)).map((column) => column.name))
    .toEqual(['case_text']);
  expect(await historyTotal(page.request, pid)).toBe(beforeHistory);
  await shoot(page, '08-fresh-ask-before-run.png');
});
