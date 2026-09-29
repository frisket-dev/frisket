import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';

const REPO_ROOT = path.basename(process.cwd()) === 'web' ? path.resolve(process.cwd(), '..') : process.cwd();
const PYTHON = process.env.FRISKET_E2E_PYTHON ?? path.join(REPO_ROOT, '.venv', 'bin', 'python');

export function extractRecordedContent(pid: string): { sheetId: number; sources: string[] } {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = String.raw`
import json, sys
from pathlib import Path
import httpx
from frisket.engine.executor import run_action_spec
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
    # Only transport is replaced: the real adapter and extraction path process
    # the recorded provider reply. No credentials or network are used.
    reply = {'choices': [{'message': {'role': 'assistant', 'content': json.dumps(record['reply'])}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 12, 'completion_tokens': 3}}
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=reply)))
    request = ActionRequest(action_id='map.extract', scope={'kind': 'sheet_rows', 'sheet_id': sheet, 'row_ids': [row]}, params=record['params'], output_names={'plaintiff': 'plaintiff'}, idempotency_key='recorded-text-citations')
    action = request.model_dump(mode='json')
    result = run_action_spec(project, action, project_id=pid, router=router)
    if result.status == 'needs_confirmation':
        action['confirmation'] = result.errors[0].details['promise_set_hash']
        result = run_action_spec(project, action, project_id=pid, router=router)
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

