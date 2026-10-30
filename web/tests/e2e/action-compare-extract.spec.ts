import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { activateComparisonCell, expectComparisonCell } from './actionCompareHelpers';

import {
  clickGroupHeader,
  clickRunButton,
  createProject,
  importCsv,
  openAction,
  openProject,
  runAndWait,
  sheetColumns,
  sheetData,
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

type ExtractField = { name: string; type: 'text' | 'date' | 'json'; description: string };
type ExtractSettings = {
  context: string;
  fields: ExtractField[];
  rows: Array<Record<string, unknown>>;
};

const outputNames = (settings: ExtractSettings) => Object.fromEntries(
  settings.fields.map(field => [field.name, field.name]),
);

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

const LARGE_SOURCE = `Case LV-001 begins with this searchable sentence. ${
  'The public record contains additional scheduling detail. '.repeat(1_400)}`;
const LARGE_CHALLENGER = `Current disposition: hearing reset to 2027-02-14. ${
  'The operative order includes a repeated public explanation. '.repeat(1_300)}`;
const LARGE_VALUE_CSV = `case_text\n${[
  LARGE_SOURCE,
  'Case LV-002 has a short current disposition.',
  'Case LV-003 has a short current disposition.',
].map(value => `"${value.replaceAll('"', '""')}"`).join('\n')}\n`;
const LARGE_A: ExtractSettings = {
  context: 'Summarize the current public disposition and preserve compact metadata.',
  fields: [
    { name: 'summary', type: 'text', description: 'Short current disposition.' },
    { name: 'metadata', type: 'json', description: 'Public docket metadata.' },
  ],
  rows: [
    { summary: 'Initial scheduling record.', metadata: { docket: 'LV-001', dates: ['2027-02-01'] } },
    { summary: 'Second scheduling record.', metadata: { docket: 'LV-002', dates: ['2027-03-01'] } },
    { summary: 'Third scheduling record.', metadata: { docket: 'LV-003', dates: ['2027-04-01'] } },
  ],
};
const LARGE_B: ExtractSettings = {
  context: 'Report the operative public disposition and include its public explanation.',
  fields: [
    { name: 'summary', type: 'text', description: 'Operative disposition with public explanation.' },
    { name: 'metadata', type: 'json', description: 'Current public docket metadata.' },
  ],
  rows: [
    { summary: LARGE_CHALLENGER, metadata: { docket: 'LV-001', dates: ['2027-02-01', '2027-02-14'] } },
    { summary: 'Second operative scheduling record.', metadata: { docket: 'LV-002', dates: ['2027-03-01'] } },
    { summary: 'Third operative scheduling record.', metadata: { docket: 'LV-003', dates: ['2027-04-01'] } },
  ],
};
const ASK_QUESTION_A = 'What is the current scheduled hearing date?';
const ASK_QUESTION_B = 'What was the first hearing date stated?';
const ASK_ANSWERS_A = ['2026-11-18', '2026-12-09', '2026-11-13'];
const ASK_ANSWERS_B = ['2026-11-04', '2026-12-02', '2026-10-30'];

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
    output_names: outputNames(settings),
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
        "output_names": {field["name"]: field["name"] for field in settings["fields"]},
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

function askAction(sheetId: number, question: string, key: string) {
  return {
    action_id: 'map.ask',
    scope: { kind: 'sheet_rows' as const, sheet_id: sheetId },
    params: {
      source: ['case_text'], model: MODEL, question, response_type: 'text',
      include_confidence: false, include_reasoning: false, context: '',
    },
    output_names: { answer: 'current_hearing' },
    idempotency_key: key,
  };
}

function primeAskCache(
  pid: string,
  sheetId: number,
  question: string,
  answers: string[],
): void {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = `
import json, sys
from pathlib import Path
from frisket.actions.system import typed_action_for_request
from frisket.ai.llm import LLMRequest, LLMResponse, ResponseCache, request_key
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.store import Project

workspace, pid, sheet_raw, action_raw, answers_raw = sys.argv[1:]
sheet_id, action, answers = int(sheet_raw), json.loads(action_raw), json.loads(answers_raw)
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    plan = build_typed_map_rows_plan(project, typed_action_for_request(action))
    spec = plan.spec_dict()
    columns = {column["name"]: column["id"] for column in project.columns(sheet_id)}
    values = project.get_values(sheet_id, columns["case_text"])
    row_ids = project.visible_row_ids(sheet_id)
    if len(row_ids) != len(answers):
        raise RuntimeError("fixture answer count does not match the imported sheet")
    cache = ResponseCache(project.path / "project.cache.db")
    try:
        for row_id, answer in zip(row_ids, answers, strict=True):
            call = plan.program.render({"case_text": values.get(row_id)}, spec)
            request = LLMRequest(model=spec["model"], messages=call.messages,
                                 schema=call.schema, max_tokens=call.max_tokens)
            data = {"answer": answer}
            cache.put(request_key(request, plan.program.version), LLMResponse(
                content=json.dumps(data), data=data, tokens_in=40, tokens_out=6,
                cost=0.0, model=spec["model"],
            ))
    finally:
        cache.close()
finally:
    project.close()
`;
  execFileSync(PYTHON, [
    '-c', script, workspace, pid, String(sheetId),
    JSON.stringify(askAction(sheetId, question, 'action-compare-ask-cache-prime')),
    JSON.stringify(answers),
  ], {
    cwd: REPO_ROOT, stdio: 'pipe', timeout: 60_000,
    env: { ...process.env, PYTHONPATH: path.join(REPO_ROOT, 'src') },
  });
}

async function shoot(page: Page, name: string): Promise<void> {
  mkdirSync(ARTIFACT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(ARTIFACT_DIR, name), animations: 'disabled', fullPage: true });
}

async function addComparisonVariant(page: Page): Promise<void> {
  await page.getByRole('button', { name: '+ ⌄', exact: true }).click();
  await page.getByRole('menuitem', { name: 'New variant', exact: true }).click();
}

async function expectComparisonRows(page: Page, text: string, scope: 'sample' | 'all'): Promise<void> {
  const toolbar = page.locator('.comparison-field-toolbar');
  await expect(toolbar.getByText(text, { exact: true })).toBeVisible();
  await expect(page.getByRole('combobox', { name: 'Comparison rows' })).toHaveValue(scope);
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

async function columnValues(
  page: Page,
  pid: string,
  sheetId: number,
  columnId: number,
  count: number,
): Promise<unknown[]> {
  const data = await sheetData(page.request, pid, sheetId, 0, count);
  return data.rows.map(row => row.cells[String(columnId)]);
}

async function configureVariant(page: Page, key: 'B' | 'C', settings: ExtractSettings) {
  await addComparisonVariant(page);
  const editor = page.getByRole('complementary', { name: `Configure variant ${key}` });
  await expect(editor).toBeVisible();
  await expect(editor.getByTestId('engine-selector')).toContainText('Gemini 3.5 Flash-Lite');
  if (key === 'B') await shoot(page, '02a-extract-editor-model.png');
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

async function configureLargeValueVariant(page: Page, settings: ExtractSettings) {
  await addComparisonVariant(page);
  const editor = page.getByRole('complementary', { name: 'Configure variant B' });
  await expect(editor).toBeVisible();
  const rows = editor.getByTestId('output-field-row');
  await expect(rows).toHaveCount(settings.fields.length);
  for (const [index, field] of settings.fields.entries()) {
    await expect(rows.nth(index).getByTestId('output-field-name')).toHaveValue(field.name);
    await expect(rows.nth(index).getByTestId('output-field-type')).toHaveValue(field.type);
    await rows.nth(index).getByTestId('output-field-description').fill(field.description);
  }
  await editor.locator('details.action-advanced > summary').click();
  await editor.getByTestId('field-context').fill(settings.context);
  await editor.getByRole('button', { name: 'Close variant editor' }).click();
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
  await expect(page.getByTestId('action-compare-variant-A')).toHaveAttribute('data-status', 'current');
  const editorB = await configureVariant(page, 'B', B);
  await shoot(page, '02-extract-variant-b-editor.png');
  await editorB.getByRole('button', { name: 'Close variant editor' }).click();

  const editorC = await configureVariant(page, 'C', C);
  await shoot(page, '03-extract-variant-c-editor.png');
  await editorC.getByRole('button', { name: 'Close variant editor' }).click();
  await expect(page.getByTestId('action-compare-variant-chips').locator('.action-compare-variant'))
    .toHaveCount(3);
  const variantsBox = await page.getByTestId('action-compare-variant-A').boundingBox();
  const runBox = await page.getByRole('button', { name: 'Run comparison', exact: true }).boundingBox();
  expect(Math.abs((variantsBox!.y + variantsBox!.height / 2) - (runBox!.y + runBox!.height / 2))).toBeLessThan(3);
  await shoot(page, '04-extract-abc-pending.png');

  await page.getByRole('button', { name: 'Run comparison', exact: true }).click();
  await approveCostGateIfShown(page);
  await expect(page.getByTestId('action-compare-variant-B'))
    .toHaveAttribute('data-status', 'ready', { timeout: 60_000 });
  await expect(page.getByTestId('action-compare-variant-C'))
    .toHaveAttribute('data-status', 'ready', { timeout: 60_000 });
  await expectComparisonCell(page, 0, 0, '24-CV-0187');
  await expectComparisonCell(page, 2, 0, 'CV 24-CV-0187');
  await shoot(page, '05-extract-abc-results.png');
  await page.getByTestId('action-compare-variant-C').hover();
  await expect(page.getByRole('tooltip')).toContainText('case_number');
  await shoot(page, '05b-setting-changes.png');

  await page.getByRole('button', { name: 'Use settings from variant C' }).click();
  const useForm = page.getByTestId('generated-action-form');
  await expect(useForm).toBeVisible();
  await expect(useForm.getByRole('button', { name: 'Replace columns', exact: true })).toBeVisible();
  await useForm.locator('details.action-advanced > summary').click();
  await expect(useForm.getByTestId('field-context')).toHaveValue(C.context);
  await expect(useForm.getByTestId('output-field-description').first())
    .toHaveValue(C.fields[0].description);
  await expect(useForm.getByRole('button', { name: 'Replace columns', exact: true })).toBeEnabled();
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

test('comparison opens bounded current and preview values plus inline JSON', async ({ page }) => {
  test.setTimeout(120_000);
  const pid = await createProject(page.request, uniqueName('e2e-action-compare-large-values'));
  await prepareOwnModelReplay(page.request, pid);
  await exposeReplayModel(page, pid);
  const sheetId = await importCsv(page.request, pid, 'large-public-records.csv', LARGE_VALUE_CSV);
  primeExtractCache(pid, sheetId, LARGE_A);
  primeExtractCache(pid, sheetId, LARGE_B);
  await runAndWait(page.request, pid, extractAction(sheetId, LARGE_A, `large-values-baseline:${pid}`));

  await openProject(page, pid, sheetId);
  const columns = await sheetColumns(page.request, pid, sheetId);
  await clickGroupHeader(page, columns, 'summary', 'metadata');
  await page.getByTestId('action-run-entry-compare').click();
  await expect(page.getByTestId('action-compare-toolbar')).toBeVisible({ timeout: 20_000 });

  await configureLargeValueVariant(page, LARGE_B);
  await page.getByRole('button', { name: 'Run comparison', exact: true }).click();
  await approveCostGateIfShown(page);
  await expect(page.getByTestId('action-compare-variant-B'))
    .toHaveAttribute('data-status', 'ready', { timeout: 60_000 });

  await activateComparisonCell(page, 0, 0);
  const currentReader = page.getByTestId('bounded-text-reader');
  await expect(currentReader).toBeVisible();
  await expect(currentReader.getByTestId('bounded-text-window')).toContainText('Case LV-001');
  await currentReader.getByRole('button', { name: 'Next' }).click();
  await expect(currentReader.getByRole('status').filter({ hasText: /Characters 16385–/ })).toBeVisible();
  await shoot(page, '09-large-current-source-reader.png');
  await page.getByRole('button', { name: 'Close full value' }).click();

  // Output cells open the comparison Review, where long values use the
  // ordinary bounded reader and fields are selected one at a time.
  await activateComparisonCell(page, 2, 0);
  const review = page.getByTestId('comparison-review');
  await expect(review).toBeVisible();
  const previewReader = review.getByTestId('bounded-text-reader').filter({ hasText: 'Current disposition' });
  await expect(previewReader).toBeVisible();
  await expect(previewReader.getByTestId('bounded-text-window')).toContainText('Current disposition');
  await expect(previewReader.getByRole('status').filter({ hasText: /Characters 1–/ })).toBeVisible();
  await previewReader.getByRole('button', { name: 'Next' }).click();
  await expect(previewReader.getByRole('status').filter({ hasText: /Characters 16385–/ })).toBeVisible();
  await shoot(page, '10-large-preview-artifact-reader.png');
  await page.getByRole('navigation', { name: 'Comparison review rows' }).getByRole('button', { name: 'Close' }).click();

  await page.getByRole('group', { name: 'Comparison field' }).getByRole('button', { name: 'metadata', exact: true }).click();
  await activateComparisonCell(page, 1, 0);
  const structured = page.getByTestId('comparison-review');
  await expect(structured.getByTestId('json-object').first()).toContainText('docket');
  await expect(structured.getByTestId('json-object').first()).toContainText('LV-001');
  await expect(structured.getByTestId('json-object').first()).toContainText('2027-02-01');
  await shoot(page, '11-inline-json-reader.png');
  await page.getByRole('navigation', { name: 'Comparison review rows' }).getByRole('button', { name: 'Close' }).click();

  await page.getByTestId('action-compare-maintab-close').click();
  await page.getByTestId('action-compare-discard-confirm').click();
  await expect(page.getByTestId('action-compare-toolbar')).toHaveCount(0);
});

test('fresh Ask can enter Compare before any action executes', async ({ page }) => {
  test.setTimeout(120_000);
  const pid = await createProject(page.request, uniqueName('e2e-action-compare-fresh-ask'));
  await prepareOwnModelReplay(page.request, pid);
  await exposeReplayModel(page, pid);
  const sheetId = await importCsv(page.request, pid, 'public-court-questions.csv', CSV);
  primeAskCache(pid, sheetId, ASK_QUESTION_A, ASK_ANSWERS_A);
  primeAskCache(pid, sheetId, ASK_QUESTION_B, ASK_ANSWERS_B);
  const beforeHistory = await historyTotal(page.request, pid);

  await openProject(page, pid, sheetId);
  await openAction(page, 'map.ask');
  const form = page.getByTestId('generated-action-form');
  const source = form.getByTestId('text-source-columns');
  await expect(form.getByTestId('text-source-mode-columns')).toHaveAttribute('aria-pressed', 'true');
  await expect(source.getByRole('button', { name: 'Remove case_text' })).toHaveCount(0);
  await expect(form.getByTestId('generated-action-run')).toBeDisabled();
  await source.click();
  await page.getByTestId('text-source-columns-menu').getByRole('option', { name: /^case_text/ }).click();
  await expect(source.getByRole('button', { name: 'Remove case_text' })).toBeVisible();
  await form.getByLabel('Question', { exact: true }).fill(ASK_QUESTION_A);
  const trigger = form.getByTestId('engine-selector').locator('.engine-selector__trigger');
  await trigger.click();
  await page.getByTestId('engine-selector-dialog')
    .locator('[data-engine-selector-choice="replay-model"]').click();
  await expect(trigger).toContainText('Gemini 3.5 Flash-Lite');
  await form.getByTestId('field-output-answer').fill('current_hearing');
  await form.getByTestId('generated-action-run-scope-menu-button').click();
  await expect(page.getByTestId('generated-action-menu-compare')).toBeVisible();
  await shoot(page, '07-fresh-ask-compare-entry.png');
  await page.getByTestId('generated-action-menu-compare').click();

  await expect(page.getByTestId('action-compare-toolbar')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('action-compare-variant-A')).toHaveAttribute('data-status', 'pending');
  await expectComparisonRows(page, '3 rows', 'sample');
  expect((await sheetColumns(page.request, pid, sheetId)).map((column) => column.name))
    .toEqual(['case_text']);
  expect(await historyTotal(page.request, pid)).toBe(beforeHistory);
  await shoot(page, '08-fresh-ask-before-run.png');

  // The fresh-action drawer remains open behind comparison until it is explicitly
  // dismissed. Close it before opening the comparison's own variant editor.
  await page.getByTestId('action-drawer-close').click();
  await addComparisonVariant(page);
  const editor = page.getByRole('complementary', { name: 'Configure variant B' });
  await editor.getByLabel('Question', { exact: true }).fill(ASK_QUESTION_B);
  await editor.getByRole('button', { name: 'Close variant editor' }).click();
  await page.getByRole('button', { name: 'Run comparison', exact: true }).click();
  await approveCostGateIfShown(page);
  await expect(page.getByTestId('action-compare-variant-A'))
    .toHaveAttribute('data-status', 'ready', { timeout: 60_000 });
  await expect(page.getByTestId('action-compare-variant-B'))
    .toHaveAttribute('data-status', 'ready', { timeout: 60_000 });
  await expectComparisonCell(page, 0, 0, ASK_ANSWERS_A[0]);
  await expectComparisonCell(page, 1, 0, ASK_ANSWERS_B[0]);
  await shoot(page, '12-fresh-ask-preview-results.png');

  await page.getByRole('button', { name: 'Use settings from variant B' }).click();
  const useForm = page.getByTestId('generated-action-form');
  await expect(useForm.getByLabel('Question', { exact: true })).toHaveValue(ASK_QUESTION_B);
  await expect(useForm.locator('.run-scope-summary')).toContainText('All 3 rows');
  await clickRunButton(page);
  await expect(page.getByTestId('run-progress')).toContainText('complete', { timeout: 60_000 });
  const columns = await sheetColumns(page.request, pid, sheetId);
  const answer = columns.find(column => column.name === 'current_hearing');
  expect(answer).toBeTruthy();
  await expect.poll(
    () => columnValues(page, pid, sheetId, answer!.id, 3),
    { timeout: 20_000 },
  ).toEqual(ASK_ANSWERS_B);
  await expect.poll(() => historyTotal(page.request, pid)).toBe(beforeHistory + 1);
  await shoot(page, '13-fresh-ask-used-all-rows.png');
});
