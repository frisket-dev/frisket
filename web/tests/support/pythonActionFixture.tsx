import { render } from '@testing-library/react';

import { actionTemplatesFromCatalog } from '../../src/actions/model';
import type { GeneratedActionCatalogEntry } from '../../src/api/types';
import { GeneratedActionForm, type GeneratedActionFormProps } from '../../src/components/action-panel/GeneratedActionForm';
import { syntheticActionCatalogEntry } from './actionCatalogFixtures';
import { completeCatalogPayload, sheetMeta } from './actionFormFixtures';
import { columnDef } from './domainFixtures';

export const PYTHON_ENTRY = syntheticActionCatalogEntry('map.python', {
  title: 'Python',
  input_schema: {
    type: 'object', additionalProperties: false,
    required: ['input_columns', 'code', 'return_schema', 'output_routes'],
    properties: {
      input_columns: { type: 'array', items: { type: 'string' }, minItems: 1 },
      code: { type: 'string', minLength: 1 },
      return_schema: { type: 'object' },
      output_routes: { type: 'array', items: { type: 'object' }, minItems: 1 },
    },
  },
  required_capabilities: ['project:write', 'unsafe:local_code'],
  writes_project: true,
  ui_hints: {
    form: 'generated', category: 'transform', dynamic_outputs: true,
    semantic_controls: { input_columns: 'columns' }, logical_outputs: [],
    source_requirements: [{ id: 'input_columns', mode: 'columns', param: 'input_columns',
      label: 'Input columns', min: 1, accepted_column_types: ['text', 'number', 'json'] }],
  },
}) as GeneratedActionCatalogEntry;

export const PYTHON_CATALOG = completeCatalogPayload([PYTHON_ENTRY]);
export const PYTHON_TEMPLATES = actionTemplatesFromCatalog(PYTHON_CATALOG);
export const PYTHON_TEMPLATE = PYTHON_TEMPLATES.find((entry) => entry.kind === 'map.python')!;
export const PYTHON_SHEET = sheetMeta([
  columnDef({ id: '1', name: 'title', type: 'text' }),
  columnDef({ id: '2', name: 'transcript', type: 'text' }),
  columnDef({ id: '3', name: 'score', type: 'number' }),
], { id: '7', rowCount: 3 });

export const resolvePythonParams: GeneratedActionFormProps['resolveParams'] = async ({ params }) => ({
  diagnostics: {},
  logical_outputs: (params.output_routes as { name: string; target: { kind: string; type?: string } }[])
    .filter((route) => route.target.kind === 'column')
    .map((route) => ({ key: route.name, column_type: route.target.type! })),
});

export function renderPythonForm(overrides: Partial<GeneratedActionFormProps> = {}) {
  return render(<GeneratedActionForm catalogEntry={PYTHON_ENTRY} actionTemplate={PYTHON_TEMPLATE}
    sheet={PYTHON_SHEET} running={false} resolveParams={resolvePythonParams}
    onExecute={() => {}} onClose={() => {}} {...overrides} />);
}
