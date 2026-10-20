import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { createProject, openCellDrawer, openProject, sheetColumns, uniqueName } from './helpers';

const ANSWER = 'Cedar Bridge manufactures public transit shelters.';
const SOURCE = 'https://example.test/company';
const UNSAFE_SOURCE = 'javascript:alert(1)';
const MARKUP = '<img src=x onerror="window.__researchInjected=1">';
const ROOT = path.basename(process.cwd()) === 'web' ? path.resolve(process.cwd(), '..') : process.cwd();

/** Persist synthetic results only; this fixture never executes a model or tool. */
function seedResearch(pid: string): { sheetId: number; runId: number; rowId: number; columnId: number } {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = String.raw`
import json, sys
from pathlib import Path
from frisket.engine.store import Project
from frisket.engine.store.current_cells import refresh_current_cells
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.value_codec import encode_stored_value

workspace, pid, answer, source, unsafe = sys.argv[1:]
project = Project(Path(workspace) / (pid + '.frisket'))
try:
    sheet = project.add_sheet('Companies')
    company = project.add_column(sheet, 'company')
    answer_column = project.add_column(sheet, 'Finding', ai_generated=True)
    sources_column = project.add_column(sheet, 'Web pages', type='json', ai_generated=True)
    row = project.add_rows(sheet, [{'company': 'Cedar Bridge'}], {'company': company})[0]
    op = project.append_op('research.answer', {'fixture': 'synthetic browser research'})
    store = RunResultStore(project)
    run = store.start_run(op, sheet, 'research.answer', model='synthetic/model',
        params={'input_columns': ['company'], 'output_names': {'answer': 'Finding', 'sources': 'Web pages'}},
        row_ids=[row], total_rows=1)
    outputs = [(answer_column, 'answer', answer), (sources_column, 'sources', [source, unsafe])]
    for column, role, value in outputs:
        project.db.execute('INSERT INTO run_output_generations (run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token) VALUES (?,?,?,?,?,?,?)',
            (run, column, role, 'synthetic-research:' + role, 'create', 'active', 'synthetic-browser-claim'))
    for column, role, value in outputs:
        project.db.execute('INSERT INTO results (run_id,row_id,column_id,value_kind,value,outcome,publication_effect) VALUES (?,?,?,?,?,?,?)',
            (run, row, column, *encode_stored_value(value), 'ok', 'publish_value'))
        project.db.execute('INSERT INTO cell_result_heads (column_id,row_id,run_id) VALUES (?,?,?)', (column, row, run))
        project.db.execute('UPDATE columns SET current_run_id=? WHERE id=?', (run, column))
    refresh_current_cells(project.db, column_ids=[answer_column, sources_column])
    project.db.execute('UPDATE runs SET completed_rows=1 WHERE id=?', (run,))
    store.finish_run(run)
    print(json.dumps({'sheetId': sheet, 'runId': run, 'rowId': row, 'columnId': answer_column}))
finally:
    project.close()
`;
  return JSON.parse(execFileSync(process.env.FRISKET_E2E_PYTHON ?? path.join(ROOT, '.venv/bin/python'),
    ['-c', script, workspace, pid, ANSWER, SOURCE, UNSAFE_SOURCE],
    { cwd: ROOT, encoding: 'utf8', timeout: 60_000 }));
}

async function fixture(page: Page, recorded = true) {
  const pid = await createProject(page.request, uniqueName('agent-research-proof'));
  const seeded = seedResearch(pid);
  const run = { id: seeded.runId, sheet_id: seeded.sheetId, action_kind: 'research.answer',
    action_name: 'Agent Research', model: 'synthetic/model', status: 'completed',
    started_at: '2026-10-20T00:00:00Z', finished_at: '2026-10-20T00:00:02Z', cost_actual: null };
  const requests: string[] = [];
  await page.route(`**/api/projects/${pid}/actions/runs/*/trace/rows/*?*`, route => {
    requests.push(route.request().url());
    const columnId = Number(new URL(route.request().url()).searchParams.get('column_id'));
    return route.fulfill({ json: {
      schema_version: 'frisket.actions.v1', action: 'run_trace_row', project_id: pid,
      run_id: seeded.runId, row_id: seeded.rowId, column_id: columnId,
      recorded, status: recorded ? 'recorded' : 'not_recorded', state: recorded ? 'recorded' : 'not_recorded',
      run, cell: { value: columnId === seeded.columnId ? ANSWER : [SOURCE, UNSAFE_SOURCE], error: null }, absence: null,
      trace: recorded ? { run_id: seeded.runId, trace_id: 'synthetic-research', action_kind: 'research.answer',
        action_name: 'Agent Research', model: 'synthetic/model', created_at: run.started_at, record_count: 1,
        row: { row_id: seeded.rowId, prompt: 'Research Cedar Bridge', raw_response: ANSWER, latency_ms: 2000,
          data: { output: { answer: ANSWER, sources: [SOURCE, UNSAFE_SOURCE] } },
          calls: [{ model: 'synthetic/model', request: { model: 'synthetic/model', messages: [{ role: 'user', content: 'Research Cedar Bridge' }],
            tools: [{ name: 'search' }], max_tokens: 800, temperature: 0.2, params: {}, mechanism: 'chat', reasoning_policy: null },
            raw_response: ANSWER, tokens_in: 30, tokens_out: 10, latency_ms: 500,
            tool_calls: [{ name: 'search', id: 'search-1', args: { query: 'Cedar Bridge manufactures' } }] }],
          research: { truncated: false, steps: [
            { tool: 'search', query: 'Cedar Bridge manufactures', provider: 'ddgs', service: 'duckduckgo', status: 'ok',
              results: [{ title: 'Cedar Bridge company', url: SOURCE, snippet: `Transit shelter maker ${MARKUP}` }], duration_ms: 12, truncated: false },
            { tool: 'fetch', url: SOURCE, status: 'ok', text: 'We manufacture public transit shelters.', duration_ms: 23, truncated: true },
            { tool: 'fetch', url: 'https://example.test/unavailable', status: 'error', text: null,
              error_code: 'fetch_failed', error: 'Synthetic page unavailable', duration_ms: 5, truncated: false },
          ] } },
      } : null,
    } });
  });
  return { pid, ...seeded, requests };
}

async function assertRecordedDetails(page: Page) {
  const modal = page.getByTestId('research-details');
  await expect(modal).toBeVisible();
  await expect(modal).toContainText('Finished');
  await expect(modal).toContainText('1 search · 1 page read');
  await expect(modal).toContainText('ddgs');
  await expect(modal).toContainText('duckduckgo');
  await expect(modal).toContainText('Synthetic page unavailable');
  await modal.getByText('Returned results (1)', { exact: true }).click();
  await expect(modal).toContainText(MARKUP);
  await modal.getByText('Returned page text', { exact: true }).click();
  await expect(modal.getByLabel('Page text', { exact: true })).toContainText('We manufacture');
  await expect(modal).toContainText('This tool result was truncated');
  await modal.getByText('Model calls (1)', { exact: true }).click();
  await modal.getByText('Call 1 · synthetic/model', { exact: true }).click();
  await expect(modal.getByLabel('Request', { exact: true })).toContainText('max_tokens');
  await expect(modal.getByLabel('Tool calls', { exact: true })).toContainText('search-1');
  await expect(modal.locator('a[href^="javascript:"]')).toHaveCount(0);
  await expect(modal.locator('img, script')).toHaveCount(0);
  return modal;
}

test('Review shows renamed companion sources and recorded tool details at wide and narrow widths', async ({ page }, testInfo) => {
  const seeded = await fixture(page);
  await openProject(page, seeded.pid, seeded.sheetId);
  await page.getByTestId('review-queue-button').click();
  const queue = page.getByTestId('review-queue');
  const research = queue.getByTestId('review-research');
  await expect(research.getByRole('link', { name: SOURCE, exact: true })).toBeVisible();
  await expect(queue.getByRole('button', { name: 'Accept Finding', exact: true })).toBeVisible();
  await expect(queue.getByRole('button', { name: 'Accept Web pages', exact: true })).toHaveCount(0);
  await research.getByRole('button', { name: 'Research details', exact: true }).click();
  const modal = page.getByTestId('research-details');
  await expect(modal.locator('.modal-title')).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath('research-review-wide.png'), animations: 'disabled' });
  await assertRecordedDetails(page);
  await page.setViewportSize({ width: 620, height: 850 });
  expect(await modal.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.keyboard.press('Escape');
  await research.getByRole('button', { name: 'Research details', exact: true }).click();
  await expect(modal.locator('.modal-title')).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath('research-review-narrow.png'), animations: 'disabled' });
  await page.keyboard.press('Escape');
  await expect(modal).toHaveCount(0);
  await expect(queue).toBeVisible();
  // StrictMode may repeat the selected-row effect; every request must still
  // address only this selected row, never eagerly load the run's other rows.
  expect(new Set(seeded.requests).size).toBe(1);
});

test('Review retains companion sources and honest absence wording when no trace was recorded', async ({ page }, testInfo) => {
  const seeded = await fixture(page, false);
  await openProject(page, seeded.pid, seeded.sheetId);
  await page.getByTestId('review-queue-button').click();
  const research = page.getByTestId('review-research');
  await expect(research.getByRole('link', { name: SOURCE, exact: true })).toBeVisible();
  await expect(research).toContainText('Tool activity was not recorded');
  await research.getByRole('button', { name: 'Research details', exact: true }).click();
  const modal = page.getByTestId('research-details');
  await expect(modal).toContainText('Tool activity was not recorded');
  await expect(modal).not.toContainText('0 searches');
  await expect(modal).toContainText(ANSWER);
  await page.screenshot({ path: testInfo.outputPath('research-missing-trace.png'), animations: 'disabled' });
});

test('Explain Research details survives hover loss and Escape preserves the parent detail view', async ({ page }, testInfo) => {
  const seeded = await fixture(page);
  await openProject(page, seeded.pid, seeded.sheetId);
  const columns = await sheetColumns(page.request, seeded.pid, seeded.sheetId);
  await openCellDrawer(page, columns, 'Finding', 0);
  const drawer = page.getByTestId('row-drawer');
  const field = drawer.locator('.row-field', { hasText: 'Finding' }).first();
  await field.hover();
  await field.getByTestId('cell-action-explain').click();
  const explain = page.getByTestId('explain-panel');
  await expect(explain).toBeVisible();
  await explain.getByRole('button', { name: 'Research details', exact: true }).click();
  await page.mouse.move(5, 5);
  const modal = page.getByTestId('research-details');
  await expect(modal.locator('.modal-title')).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath('research-explain-wide.png'), animations: 'disabled' });
  await assertRecordedDetails(page);
  await page.keyboard.press('Escape');
  await expect(modal).toHaveCount(0);
  await expect(drawer).toBeVisible();
  await expect(explain).toBeVisible();
  await explain.getByTestId('explain-panel-close').click();
  const sourcesField = drawer.getByTestId('row-field-Web pages');
  await sourcesField.hover();
  await sourcesField.getByTestId('cell-action-explain').click();
  await explain.getByRole('button', { name: 'Research details', exact: true }).click();
  await expect(modal.getByLabel('Answer', { exact: true })).toHaveText(ANSWER);
  await expect(modal.getByRole('region', { name: 'Sources encountered', exact: true })
    .getByRole('link', { name: SOURCE, exact: true })).toBeVisible();
});
