import { useEffect, useState } from 'react';

import type { OutputJsonSchema } from '../../api/open';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import type { GeneratedActionParams } from '../../generated/actionTypes';
import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';
import { PanelSelect } from '../PanelSelect';
import { McpToolsSelector, type McpToolsSelectorServer } from './McpToolsSelector';
import { StructuredFieldsEditor } from './StructuredFieldsEditor';

type ExtractField = GeneratedActionParams['map.extract']['fields'][number];
const FIELD_TYPES: readonly ExtractField['type'][] = [
  'text', 'score', 'integer', 'number', 'boolean', 'category', 'date', 'list', 'json',
];

export function ExtractFieldsEditor({ fields, onChange, optional = false }: {
  fields: ExtractField[];
  onChange(fields: ExtractField[]): void;
  optional?: boolean;
}) {
  return <StructuredFieldsEditor actionKind={optional ? 'map.find' : 'map.extract'}
    fieldTypes={FIELD_TYPES} maxFields={optional ? 63 : 64} minFields={optional ? 0 : 1}
    value={fields.map((field) => ({
      name: field.name, type: field.type, description: field.description ?? '',
      ...(field.labels?.length ? { labels: field.labels } : {}),
      // The compact editor displays its known schema properties and retains
      // the complete JSON schema when unrelated field details are edited.
      ...(field.items ? { items: field.items as unknown as OutputJsonSchema } : {}),
      ...(field.properties ? { properties: field.properties as unknown as Record<string, OutputJsonSchema> } : {}),
      ...(field.required ? { required: true } : {}),
    }))}
    onChange={(value) => onChange(value.map((field) => {
      if (!FIELD_TYPES.includes(field.type as ExtractField['type'])) throw new Error('Unknown extraction field type');
      return {
        name: field.name, type: field.type as ExtractField['type'], description: field.description,
        ...(field.type === 'category' ? { labels: field.labels ?? [] } : {}),
        ...(field.type === 'list' ? { items: field.items as unknown as ExtractField['items'] } : {}),
        ...(field.type === 'json' ? { properties: field.properties as unknown as ExtractField['properties'] } : {}),
        ...(field.required ? { required: true } : {}),
      };
    }))} />;
}

export function ExtractParamsBody({ params, setParams, errors, Field }:
  GeneratedActionParamsBodyProps<'map.extract'>) {
  const groundingEnabled = Boolean(params.grounding?.enabled);
  const citationRequired = Boolean(
    params.grounding?.citation_required || params.evidence_policy?.citation_required,
  );
  const citationMode = groundingEnabled
    ? (citationRequired ? 'require' : 'cite')
    : 'none';
  const setCitationMode = (mode: string) => {
    const citationRequired = mode === 'require';
    setParams({
      ...params,
      grounding: {
        ...(params.grounding ?? {}),
        enabled: mode !== 'none',
        citation_required: citationRequired,
      },
      evidence_policy: {
        ...(params.evidence_policy ?? {}),
        citation_required: citationRequired,
      },
    });
  };
  return <>
    <Field name="source" />
    <ExtractFieldsEditor fields={params.fields ?? []}
      onChange={(fields) => setParams({ ...params, fields })} />
    {errors.fields?.message && <p className="form-error" role="alert">{errors.fields.message}</p>}
    <Field name="instruction" />
    <Field name="model" />
    <Field name="include_confidence" />
    <details className="action-advanced"><summary>Grounding and context</summary>
      <label className="field-group">
        <span className="form-label">Citations</span>
        <PanelSelect testId="field-citation_mode" value={citationMode}
          onValueChange={setCitationMode}
          options={[
            { value: 'none', label: 'Off', description: 'Do not request evidence grounding.' },
            { value: 'cite', label: 'Cite sources', description: 'Keep values when evidence is unavailable.' },
            { value: 'require', label: 'Require citations', description: 'Withhold values without usable evidence.' },
          ]} />
        <span className="form-hint">Require citations withholds or fails fields without usable evidence.</span>
      </label>
      <Field name="source_document_columns" />
      <Field name="grounding" />
      <Field name="evidence_policy" />
      <Field name="context" />
    </details>
  </>;
}

export function FindParamsBody({ params, setParams, errors, Field }:
  GeneratedActionParamsBodyProps<'map.find'>) {
  return <>
    <Field name="source" />
    <Field name="instruction" />
    <ExtractFieldsEditor fields={params.fields ?? []} optional
      onChange={(fields) => setParams({ ...params, fields })} />
    {errors.fields?.message && <p className="form-error" role="alert">{errors.fields.message}</p>}
    <Field name="model" />
  </>;
}

export function McpExtractParamsBody({ params, setParams, errors, Field }:
  GeneratedActionParamsBodyProps<'map.mcp_extract'>) {
  const { projectApi } = useWorkspaceStores();
  const [servers, setServers] = useState<McpToolsSelectorServer[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    projectApi.listMcpServers().then((value) => {
      if (active) setServers(value.map((server) => ({
        id: server.id, name: server.name, enabled: server.enabled,
        lastDiscoveredToolCount: server.last_discovered_tool_count ?? undefined,
      })));
    }).catch(() => { if (active) setProblem('MCP servers could not be loaded.'); });
    return () => { active = false; };
  }, [projectApi]);
  return <>
    <Field name="source" />
    <ExtractFieldsEditor fields={params.fields ?? []}
      onChange={(fields) => setParams({ ...params, fields })} />
    {errors.fields?.message && <p className="form-error" role="alert">{errors.fields.message}</p>}
    <Field name="instruction" />
    <McpToolsSelector servers={servers} mcpServerIds={params.mcp_server_ids ?? []}
      onMcpServerIdsChange={(mcp_server_ids) => setParams({ ...params, mcp_server_ids })} />
    {(problem || errors.mcp_server_ids?.message) && <p className="form-error" role="alert">
      {problem ?? errors.mcp_server_ids?.message}
    </p>}
    <Field name="model" />
    <Field name="include_confidence" />
    <Field name="context" />
  </>;
}
