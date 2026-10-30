"""Render the actual Extract requests from an imported guide project, offline."""
import dataclasses
import json
import sys
from pathlib import Path

from frisket.actions.system import typed_action_for_request
from frisket.ai.llm import LLMRequest
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.store import Project

workspace, project_id, sheet_id, plan_file, output, *authored = sys.argv[1:]
sheet_id = int(sheet_id)
recipe = json.loads(Path(plan_file).read_text())
settings = {item['id']: item for step in recipe['steps'] if step['id'] == 'extract' for item in step['settings']}
fields = settings['fields']['value']
action = {
    'action_id': 'map.extract',
    'scope': {'kind': 'sheet_rows', 'sheet_id': sheet_id},
    'params': {'source': ['text'], 'engine': 'llm', 'model': 'gemini/gemini-3.5-flash-lite',
               'instruction': settings['instruction']['value'], 'fields': fields,
               'include_confidence': False, 'grounding': settings['citations']['value'],
               'evidence_policy': {'citation_required': False}},
    'output_names': {field['name']: field['name'] for field in fields},
    'idempotency_key': 'docs-render-court-requests',
}
if authored:
    action['params'] = json.loads(Path(authored[0]).read_text())['action']['params']
project = Project(Path(workspace) / f'{project_id}.frisket')
try:
    plan = build_typed_map_rows_plan(project, typed_action_for_request(action))
    spec = plan.spec_dict()
    columns = {column['name']: column['id'] for column in project.columns(sheet_id)}
    names = project.get_values(sheet_id, columns['filename'])
    values = project.get_values(sheet_id, columns['text'])
    requests = []
    for row_id in project.visible_row_ids(sheet_id):
        value = values.get(row_id)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f'prepared text unavailable for {names.get(row_id)}')
        call = plan.program.render({'text': value}, spec)
        request = LLMRequest(model=spec['model'], messages=call.messages, schema=call.schema, max_tokens=call.max_tokens)
        requests.append({'filename': names[row_id], 'recipe_version': plan.program.version, 'request': dataclasses.asdict(request)})
    if len(requests) != 6:
        raise RuntimeError(f'expected six filings, got {len(requests)}')
    Path(output).write_text(json.dumps(requests, indent=2) + '\n')
finally:
    project.close()
