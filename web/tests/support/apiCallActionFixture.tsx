import { render } from '@testing-library/react';
import { actionTemplatesFromCatalog } from '../../src/actions/model';
import type { GeneratedActionCatalogEntry } from '../../src/api/types';
import { GeneratedActionForm, type GeneratedActionFormProps } from '../../src/components/action-panel/GeneratedActionForm';
import { syntheticActionCatalogEntry } from './actionCatalogFixtures';
import { completeCatalogPayload, sheetMeta } from './actionFormFixtures';
import { columnDef } from './domainFixtures';

export const API_CALL_ENTRY = syntheticActionCatalogEntry('map.api_call', {
  title: 'Call an API',
  input_schema: { type: 'object', additionalProperties: false, required: ['request'],
    properties: { request: { type: 'object', required: ['url'], properties: {
      url: { type: 'string', minLength: 1 }, method: { type: 'string', default: 'GET' },
      headers: { type: 'array', default: [] }, query_params: { type: 'array', default: [] },
      form_body: { type: 'array', default: [] }, cookies: { type: 'array', default: [] },
      body_mode: { type: 'string', default: 'none' }, body: { type: 'string', default: '' },
      content_type: { default: null }, timeout: { type: 'number', default: 30 },
      max_requests_per_second: { default: null }, follow_redirects: { type: 'boolean', default: true },
    } } } },
  required_capabilities: ['project:write', 'external:api_call'], writes_project: true,
  cost_policy: { kind: 'unknown', requires_confirmation: true },
  ui_hints: { form: 'generated', category: 'enrich', semantic_controls: {},
    logical_outputs: [{ key: 'api_result', column_type: 'json' }],
  },
}) as GeneratedActionCatalogEntry;
export const API_CALL_CATALOG = completeCatalogPayload([API_CALL_ENTRY]);
export const API_CALL_TEMPLATES = actionTemplatesFromCatalog(API_CALL_CATALOG);
export const API_CALL_TEMPLATE = API_CALL_TEMPLATES.find((entry) => entry.kind === 'map.api_call')!;
export const API_CALL_SHEET = sheetMeta([
  columnDef({ id: '1', name: 'customer_id', type: 'text' }),
], { id: '7', name: 'Customers', rowCount: 12 });
export const resolveApiCallParams: GeneratedActionFormProps['resolveParams'] = async () => ({
  diagnostics: {}, logical_outputs: API_CALL_ENTRY.ui_hints.logical_outputs,
});
export function renderApiCallForm(overrides: Partial<GeneratedActionFormProps> = {}) {
  return render(<GeneratedActionForm catalogEntry={API_CALL_ENTRY} actionTemplate={API_CALL_TEMPLATE}
    sheet={API_CALL_SHEET} running={false} resolveParams={resolveApiCallParams}
    onExecute={() => {}} onClose={() => {}} {...overrides} />);
}
