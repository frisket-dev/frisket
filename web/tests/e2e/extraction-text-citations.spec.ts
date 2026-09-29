import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createProject, dblclickCell, openProject, sheetColumns, uniqueName } from './helpers';

const REPO_ROOT = path.basename(process.cwd()) === 'web' ? path.resolve(process.cwd(), '..') : process.cwd();
const PYTHON = path.join(REPO_ROOT, '.venv', 'bin', 'python');

function extractRecordedContent(pid: string): { sheetId: number; sources: string[] } {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = String.raw`
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests' / 'engine'))
from test_model_rows_actions import _DataAdapter
from executor_harness import run_action_with_confirmation
from frisket.actions.types import ActionRequest
from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project
from frisket.engine.store.evidence import list_cell_evidence, resolve_evidence_viewer

workspace, pid = sys.argv[1:]
record = json.loads(Path('web/tests/fixtures/text-citation-live.json').read_text())
project = Project(Path(workspace) / (pid + '.frisket'))
try:
    sheet = project.add_sheet('Court filings')
    columns = {name: project.add_column(sheet, name, 'text') for name in record['inputs']}
    row = project.add_rows(sheet, [record['inputs']], columns)[0]
    router = ModelRouter(keys={'gemini': 'recorded'}, cache=None, cache_mode='off', use_env_keys=False)
    router._adapters['gemini'] = _DataAdapter([], record['reply'])
    request = ActionRequest(action_id='map.extract', scope={'kind': 'sheet_rows', 'sheet_id': sheet, 'row_ids': [row]}, params=record['params'], output_names={'plaintiff': 'plaintiff'}, idempotency_key='recorded-text-citations')
    result = run_action_with_confirmation(project, request.model_dump(mode='json'), project_id=pid, router=router)
    assert result.status == 'completed', result.errors
    output = next(c for c in project.columns(sheet) if c['name'] == 'plaintiff')
    links = list_cell_evidence(project, sheet_id=sheet, row_id=row, column_id=output['id'])['links']
    sources = []
    for link in links:
        payload = resolve_evidence_viewer(project, link['stable_id'])
        assert not payload['link']['text_layer_hash_mismatch']
        sources.extend(a['text_context']['text'] for a in payload['artifacts'] if a.get('text_context'))
    assert set(sources) == set(record['inputs'].values())
    print(json.dumps({'sheetId': sheet, 'sources': sources}))
finally:
    project.close()
`;
  return JSON.parse(execFileSync(PYTHON, ['-c', script, workspace, pid], {
    cwd: REPO_ROOT, encoding: 'utf8', timeout: 60_000,
    env: { ...process.env, FRISKET_CACHE_MODE: 'off' },
  })) as { sheetId: number; sources: string[] };
}

test('Recorded live extraction opens all cited source text with newline and repeated matches', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('text-citation-live-replay'));
  const seeded = extractRecordedContent(pid);
  const columns = await sheetColumns(page.request, pid, seeded.sheetId);
  await openProject(page, pid, seeded.sheetId);
  await dblclickCell(page, columns, 'plaintiff', 0);
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  const citations = drawer.getByTestId('cell-evidence-open-plaintiff');
  await expect(citations).toHaveCount(seeded.sources.length);
  const seen: string[] = [];
  for (let index = 0; index < seeded.sources.length; index += 1) {
    await citations.nth(index).click();
    const viewer = page.getByTestId('evidence-viewer');
    await expect(viewer).toBeVisible();
    const text = viewer.getByTestId('evidence-text-body');
    const source = await text.textContent();
    expect(source).not.toBeNull();
    expect(seeded.sources).toContain(source);
    seen.push(source!);
    const marks = viewer.getByTestId('evidence-text-highlight');
    const count = await marks.count();
    expect(count).toBeGreaterThanOrEqual(2);
    for (const mark of await marks.allTextContents()) expect(mark.replace(/\s+/gu, ' ')).toBe('Leena Patel');
    await expect(viewer).not.toContainText('no_word_stream');
    await expect(viewer.getByTestId('evidence-viewer-detail-pane')).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath(`source-${index}.png`) });
    await viewer.getByRole('button', { name: 'Close evidence viewer' }).click();
  }
  expect(new Set(seen)).toEqual(new Set(seeded.sources));
});
