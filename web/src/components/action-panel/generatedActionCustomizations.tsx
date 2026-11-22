import '../PanelPrimitives.css';
import { Button } from '../Button';
import { TextInput } from '../FormControls';
import { PanelSelect } from '../PanelSelect';
/* eslint-disable react-refresh/only-export-components -- sparse first-party field bindings */
import { Plus, Trash2 } from 'lucide-react';
import type { ReactNode } from 'react';
import type { ActionFieldProps } from '../../generated/actionUI';

import type { CanonicalActionDraft, CanonicalActionParamValue } from '../../actions/canonicalActionDraft';
import type { SheetMeta } from '../../api/open';
import type { GeneratedActionParams } from '../../generated/actionTypes';
import { CleanDatesForm } from '../CleanDatesForm';
import { CombineForm } from '../resolve/CombineForm';
import { ReplaceForm } from '../resolve/ReplaceForm';
import { SubstituteForm } from '../resolve/SubstituteForm';
import { PythonCodeField } from './ActionParams';
import { ApiCallParamsBody } from './ApiCallParamsBody';
import { JoinParamsBody } from './JoinParamsBody';
import { SpatialJoinParamsBody } from './SpatialJoinParamsBody';
import { SemanticJoinParamsBody } from './SemanticJoinParamsBody';
import { MediaSegmentsParamsBody, TranscriptSegmentsParamsBody } from './TranscriptSegmentsParamsBody';
import { TemporalExtractParamsBody } from './TemporalExtractParamsBody';
import { DownloadMediaOptions } from './DownloadMediaOptions';
import { ClusterParamsBody } from './ClusterParamsBody';
import { PageCaptureParamsBody } from './PageCaptureParamsBody';
import { NerParamsBody } from './NerParamsBody';
import { ClassifyParamsBody } from './ClassifyParamsBody';
import { ExtractFieldsEditor, ExtractParamsBody } from './ExtractParamsBody';
import { ProjectMcpToolsSelector } from './McpToolsSelector';
import { TranslateParamsBody } from './TranslateParamsBody';
import { RECOMMENDED_SPACY_TYPES } from './nerLabelModel';
import { extractFieldFromQuestion } from './outputFieldModel';
import { FetchUrlParamsBody, VideoFramesParamsBody } from './MediaParamsBodies';
import { OcrParamsBody, TranscribeParamsBody } from './MediaReadParamsBodies';
import { ActionPromptField } from './ActionPromptField';
import { ListTableParamsBody } from './ListTableParamsBody';
import { EntityTableParamsBody } from './EntityTableParamsBody';
import { declarativeParamsBody } from './declarativeParamsBody';
import type {
  DynamicGeneratedActionParamsBody,
  GeneratedActionParamsBody,
  GeneratedActionParamsBodyProps,
} from './GeneratedActionParamsBody';
import './api-call-form.css';

type ColumnsFromJsonRoute = GeneratedActionParams['map.columns_from_json']['routes'][number];

export interface GeneratedActionFieldProps
  extends ActionFieldProps<CanonicalActionParamValue> {
  value: CanonicalActionParamValue | undefined;
  sheet: SheetMeta | null;
  onChange(value: CanonicalActionParamValue): void;
}

interface GeneratedActionCustomizationBase {
  /** Visible defaults for a fresh editor only; never rewrite saved Params. */
  initialParams?: CanonicalActionDraft;
  initialPromptParams?(prompt: string): CanonicalActionDraft;
  defaultSheetName?: string;
  outputNamesReadOnly?: true;
  hiddenOutputNameKeys?: readonly string[];
  fields?: Readonly<Partial<Record<string, (props: GeneratedActionFieldProps) => ReactNode>>>;
  /** Params whose custom body already renders their diagnostic beside a control.
   * Other custom-body Params use the host footer as a safe fallback. */
  inlineDiagnosticFields?: readonly string[] | ((params: CanonicalActionDraft) => readonly string[]);
  fieldOrder?: readonly string[];
  defaultOutputName?(key: string, draft: CanonicalActionDraft): string | undefined;
  outputLabel?(key: string): string | undefined;
  outputPrefix?: {
    label: string | ((outputs: readonly { key: string }[]) => string);
    initialValue(names?: Readonly<Record<string, string>>): string;
    outputName(key: string, prefix: string, outputs: readonly { key: string }[]): string;
    keys?: readonly string[];
    hint?: ReactNode;
  };
  primaryLabel?: string;
  primaryTestId?: string;
  freePublicApiLabel?: string;
}

export interface GeneratedActionCustomization extends GeneratedActionCustomizationBase {
  body?: DynamicGeneratedActionParamsBody;
}

interface StoredGeneratedActionCustomization extends GeneratedActionCustomizationBase {
  /** Exact action/body association is checked at each declaration below;
   * the catalog-id lookup is the one dynamic boundary. */
  body?: unknown;
}

type GeneratedActionKey = keyof GeneratedActionParams;
type GeneratedFieldName<K extends GeneratedActionKey> = Extract<keyof GeneratedActionParams[K], string>;
type ExactGeneratedActionCustomization<K extends GeneratedActionKey> =
  Omit<GeneratedActionCustomizationBase, 'fields' | 'fieldOrder' | 'initialParams' | 'inlineDiagnosticFields'> & {
    initialParams?: Partial<GeneratedActionParams[K]>;
    fields?: Readonly<Partial<Record<GeneratedFieldName<K>,
      (props: GeneratedActionFieldProps) => ReactNode>>>;
    fieldOrder?: readonly GeneratedFieldName<K>[];
    inlineDiagnosticFields?: readonly GeneratedFieldName<K>[]
      | ((params: CanonicalActionDraft) => readonly GeneratedFieldName<K>[]);
    body?: GeneratedActionParamsBody<K>;
  };
type ExactGeneratedActionCustomizationMap = {
  [K in GeneratedActionKey]?: ExactGeneratedActionCustomization<K>;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function routes(value: unknown): ColumnsFromJsonRoute[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry) => (
    isRecord(entry) && typeof entry.name === 'string' && typeof entry.path === 'string'
      ? [{ name: entry.name, path: entry.path }]
      : []
  ));
}

function ColumnsFromJsonRoutesField({ value, onChange }: GeneratedActionFieldProps) {
  const authored = routes(value);
  const visible = authored.length > 0 ? authored : [{ name: '', path: '' }];
  const update = (index: number, key: keyof ColumnsFromJsonRoute, next: string) => {
    onChange(visible.map((route, routeIndex) => (
      routeIndex === index ? { ...route, [key]: next } : route
    )));
  };

  return (
    <div className="field-group" data-testid="columns-from-json-routes">
      <div className="api-pair-heading">
        <span className="field-labels">Fields to extract</span>
        <Button type="button" data-testid="columns-from-json-add-route"
          onClick={() => onChange([...visible, { name: '', path: '' }])}>
          <Plus size={12} /> Add field
        </Button>
      </div>
      <span className="form-hint">Use paths like $.address.city or $.items[0].name.</span>
      {visible.map((route, index) => (
        <div className="api-route-row" key={`route-${index}`}>
          <TextInput aria-label={`Field ${index + 1} JSON path`}
            placeholder="$.path.to.value" value={route.path}
            data-testid={`columns-from-json-path-${index}`}
            onChange={(event) => update(index, 'path', event.target.value)} />
          <TextInput aria-label={`Field ${index + 1} output name`}
            placeholder="Output name" value={route.name}
            data-testid={`columns-from-json-name-${index}`}
            onChange={(event) => update(index, 'name', event.target.value)} />
          <button type="button" className="icon-btn api-pair-remove"
            aria-label={`Remove field ${index + 1}`}
            onClick={() => onChange(visible.length === 1
              ? [] : visible.filter((_, routeIndex) => routeIndex !== index))}>
            <Trash2 size={13} />
          </button>
        </div>
      ))}
      <div className="api-local-note">
        Reads the stored JSON locally. It does not call the API again.
      </div>
    </div>
  );
}

function CensusGeographyField({ id, testid, label, value, onChange }: GeneratedActionFieldProps) {
  return <div className="param-row">
    <label className="form-label" htmlFor={id}>{label}</label>
    <PanelSelect id={id} testId={testid} value={typeof value === 'string' ? value : 'tract'}
      onValueChange={onChange} options={[
        { value: 'tract', label: 'Census tract' },
        { value: 'block_group', label: 'Census block group' },
      ]} />
  </div>;
}

function PromptField({ label, value, onChange }: GeneratedActionFieldProps) {
  return <ActionPromptField label={label} value={typeof value === 'string' ? value : ''}
    rows={5} onChange={onChange} />;
}

function extractFields<K extends 'map.find' | 'map.mcp_extract' | 'research.extract'>(optional = false) {
  return ({ params, setParams, errors, structureLocked, lockReason }: GeneratedActionParamsBodyProps<K>) => (
    <ExtractFieldsEditor fields={params.fields ?? []} optional={optional}
      structureLocked={structureLocked} lockReason={lockReason}
      error={errors.fields?.message} onChange={(fields) => setParams({ ...params, fields })} />
  );
}

function suffixedSourceOutput(outputKey: string, suffix: string) {
  return (key: string, draft: CanonicalActionDraft) => (
    key === outputKey && typeof draft.source === 'string' ? `${draft.source}_${suffix}` : undefined
  );
}

const EXACT_CUSTOMIZATIONS = {
  'web.capture_page': { body: PageCaptureParamsBody, primaryLabel: 'Capture pages',
    inlineDiagnosticFields: ['source', 'max_bytes', 'timeout_ms'],
    defaultSheetName: 'Links', outputLabel: (key: string) => key === 'page' ? 'Snapshot' : undefined },
  'cluster.values': { body: ClusterParamsBody, primaryLabel: 'Apply groups',
    primaryTestId: 'cluster-commit-button', defaultOutputName: (_key, draft) =>
      typeof draft.source === 'string' ? `${draft.source}_canonical` : 'canonical' },
  'join.semantic': { body: SemanticJoinParamsBody, primaryLabel: 'Match rows', defaultSheetName: 'Matches',
    initialParams: { carry: [] } },
  'derive.join': { body: JoinParamsBody, primaryLabel: 'Join tables', defaultSheetName: 'Joined',
    outputLabel: (key: string) => key === '_merge' ? 'Indicator' : undefined },
  'derive.spatial_join': { body: SpatialJoinParamsBody, primaryLabel: 'Spatial join', defaultSheetName: 'Spatial joined',
    outputLabel: (key: string) => key === '_merge' ? 'Indicator' : undefined },
  'media.ytdlp_download': {
    body: DownloadMediaOptions,
    primaryLabel: 'Download media',
    initialParams: { media_type: 'video' },
    defaultOutputName: (key: string) => key === 'audio' || key === 'video' ? 'media' : `media_${key}`,
  },
  'media.enclosure_materialize': { primaryLabel: 'Download enclosures' },
  'temporal.extract_range': { body: TemporalExtractParamsBody, primaryLabel: 'Extract range',
    inlineDiagnosticFields: ['source'] },
  'derive.temporal_segments': { body: MediaSegmentsParamsBody, primaryLabel: 'Split into segments',
    defaultSheetName: 'Segments' },
  'media.fetch_url': { body: FetchUrlParamsBody, primaryLabel: 'Fetch URL', inlineDiagnosticFields: ['source'] },
  'media.video_frames': { body: VideoFramesParamsBody, initialParams: { max_dimension: 720 },
    inlineDiagnosticFields: ['source', 'sampling', 'max_dimension'],
    primaryLabel: 'Extract frames' },
  'media.extract_pdf_tables': {
    body: declarativeParamsBody<'media.extract_pdf_tables'>([
      { field: 'source', label: 'PDF' },
      { field: 'table_mode', label: 'Table mode',
        hint: 'Auto lets Natural PDF choose. Stream reads whitespace-aligned tables; lattice follows drawn ruled lines.' },
      { advanced: 'Pages and extraction options', testId: 'pdf-table-extraction-options',
        items: [{ field: 'extract_table', label: 'Extraction options' }] },
    ]),
    primaryLabel: 'Extract PDF tables', inlineDiagnosticFields: ['source', 'table_mode', 'extract_table'],
    outputLabel: () => 'Tables',
  },
  'derive.transcript_segments': { body: TranscriptSegmentsParamsBody, primaryLabel: 'Create transcript segments',
    inlineDiagnosticFields: ['source'] },
  'derive.link_table': {
    body: declarativeParamsBody<'derive.link_table'>([
      ({ params, setParams }) => <label className="field-group form-field">
        <span className="form-label">Semantic join receipt</span>
        <TextInput data-testid="link-table-receipt" value={params.source?.receipt_id ?? ''}
          onChange={(event) => setParams({ ...params, source: { kind: 'semantic_join', receipt_id: event.target.value } })} />
      </label>,
      { hint: 'Creates a link table from a completed semantic join receipt. Matching is not run again.' },
      'include_unmatched',
    ]),
    primaryLabel: 'Create link table', inlineDiagnosticFields: ['include_unmatched'],
  },
  'map.find_topic_sections': {
    body: declarativeParamsBody<'map.find_topic_sections'>([
      { field: 'source', label: 'Timestamped transcript' }, 'engine',
      ({ params, setParams }) => <label className="field-group form-field">
        <span className="form-label">Detail</span>
        <PanelSelect data-testid="field-detail"
          value={typeof params.settings?.detail === 'string' ? params.settings.detail : 'balanced'}
          onChange={(event) => setParams({ ...params, settings: { ...params.settings, detail: event.target.value } })}>
          <option value="fewer">Fewer sections</option>
          <option value="balanced">Balanced</option>
          <option value="more">More sections</option>
        </PanelSelect>
      </label>,
      { hint: 'Find topic changes locally and save editable timeline ranges.' },
      { advanced: 'Segmentation settings', items: ['settings'] },
    ]),
    inlineDiagnosticFields: ['source', 'engine', 'settings'],
  },
  'map.api_call': { body: ApiCallParamsBody, primaryLabel: 'Run API calls', outputLabel: () => 'Response' },
  'map.python': {
    initialParams: {
      input_columns: [],
      code: 'result = {"computed": sum(len(str(v)) for v in row.values() if v)}',
      return_schema: { type: 'object' },
      output_routes: [{ name: 'computed', path: '$', target: { kind: 'column', type: 'json' } }],
    },
    body: declarativeParamsBody<'map.python'>([
      { field: 'input_columns', label: 'Inputs' },
      { field: 'code', label: 'Python snippet' },
      { advanced: 'Return schema and output routes', items: [
        { hint: 'Routes select values from result. Column routes appear below; named results and receipt values remain hidden. Final column names do not change route keys.' },
        'return_schema', 'output_routes',
      ] },
    ]),
    inlineDiagnosticFields: ['input_columns', 'code', 'return_schema', 'output_routes'],
    fields: {
      code: ({ label, value, onChange, testid }) => <PythonCodeField
        param={{ name: 'code', label, input: 'textarea' }}
        value={typeof value === 'string' ? value : ''} onChange={onChange} testid={testid} />,
    },
  },
  'enrich.geocode': {
    body: declarativeParamsBody<'enrich.geocode'>([
      { field: 'source', label: 'Address', hintTestId: 'action-input-summary',
        hint: 'Use an address column or compose an address from multiple columns.' },
      { field: 'engine', hint: 'Auto uses OpenCage when OPENCAGE_API_KEY is configured, else Nominatim.' },
      ({ params, setParams }) => <div className="action-io-summary">
        <div className="form-label">Outputs</div>
        <div className="form-hint" data-testid="action-output-summary">
          <strong>geo_point</strong> is the map column; <strong>formatted_address</strong> is the geocoder-normalized address.
          {params.include_lat_lon && ' latitude and longitude duplicate the point as scalar columns.'}
        </div>
        <label className="form-check">
          <input type="checkbox" data-testid="include-lat-lon-columns" checked={params.include_lat_lon ?? false}
            onChange={(event) => setParams({ ...params, include_lat_lon: event.target.checked })} />
          Also include latitude and longitude columns
        </label>
      </div>,
    ]),
    freePublicApiLabel: 'Free public API request', inlineDiagnosticFields: ['source', 'engine'],
  },
  'enrich.census_demographics': {
    body: declarativeParamsBody<'enrich.census_demographics'>([
      ({ sheet, onNavigateToAction }) => (sheet?.columns ?? []).every((column) => column.type !== 'geo_point')
        ? <div className="empty-inline" data-testid="census-no-geo-point">
            <span>No compatible <code>geo_point</code> columns.</span>
            <Button type="button" onClick={() => onNavigateToAction?.('enrich.geocode')}>Run Geocode first</Button>
          </div>
        : null,
      { field: 'source', label: 'Location' }, 'geography', 'include_moe',
    ]),
    inlineDiagnosticFields: ['source', 'geography', 'include_moe'],
    fields: { geography: CensusGeographyField }, freePublicApiLabel: 'US Census ACS enrichment',
  },
  'resolve.entities': {
    body: EntityTableParamsBody,
    primaryLabel: 'Create entity sheet',
    outputNamesReadOnly: true,
  },
  'media.extract_metadata': {
    outputPrefix: {
      label: (outputs) => outputs.length === 1 ? 'Metadata' : 'Metadata prefix',
      initialValue: (names) => {
        if (!names || !Object.keys(names).length) return 'meta';
        const details = names.details ?? '';
        return Object.keys(names).length === 1 ? details
          : details.endsWith('_details') ? details.slice(0, -'_details'.length) : '';
      },
      outputName: (key, prefix, outputs) => !prefix ? ''
        : key === 'details' && outputs.length === 1 ? prefix : `${prefix}_${key}`,
    },
  },
  'media.ocr': {
    body: OcrParamsBody,
    inlineDiagnosticFields: ['source', 'engine', 'dpi', 'searchable_pdf'],
    defaultOutputName: (key) => key === 'text' ? 'ocr_text' : key === 'blocks' ? 'ocr_text_boxes' : key === 'pdf' ? 'searchable_pdf' : undefined,
    hiddenOutputNameKeys: ['text', 'blocks'],
    outputLabel: (key) => key === 'pdf' ? 'Searchable PDF' : undefined,
    outputPrefix: {
      label: 'Text result',
      initialValue: (names) => names?.text ?? 'ocr_text',
      outputName: (key, prefix) => key === 'text' ? prefix : `${prefix}_boxes`,
      keys: ['text', 'blocks'],
      hint: null,
    },
  },
  'media.transcribe': {
    body: TranscribeParamsBody,
    inlineDiagnosticFields: ['source', 'engine'],
    // Show defaults in controls, but only author options the user changes.
    // Explicit unsupported knobs (even false/null) correctly refuse server-side.
    initialParams: {
      language: undefined, model_size: undefined, context: undefined,
      vad: undefined, clean: undefined, diarize: undefined,
      num_speakers: undefined, min_speakers: undefined, max_speakers: undefined,
    },
    defaultOutputName: (key) => key === 'text' ? 'transcript' : key === 'segments' ? 'transcript_segments' : undefined,
    outputLabel: (key) => key === 'text' ? 'Transcript' : key === 'segments' ? 'Timestamped segments' : 'Detected language',
  },
  'derive.table_from_list': {
    body: ListTableParamsBody satisfies GeneratedActionParamsBody<'derive.table_from_list'>,
  },
  'map.ask': {
    fieldOrder: ['source', 'question', 'response_type', 'include_confidence', 'include_reasoning', 'model', 'context'],
    fields: { question: PromptField },
    outputLabel: (key) => key === 'answer' ? 'Answer' : key === 'answer_confidence' ? 'Confidence' : key === 'answer_reasoning' ? 'Reasoning' : undefined,
  },
  'map.ner': {
    body: NerParamsBody,
    inlineDiagnosticFields: ['source', 'engine', 'labels'],
    initialParams: { labels: [...RECOMMENDED_SPACY_TYPES] },
    outputLabel: () => 'Entities',
  },
  'map.classify': {
    body: ClassifyParamsBody,
    inlineDiagnosticFields: (params): readonly GeneratedFieldName<'map.classify'>[] => [
      'source', 'engine', 'fields', 'context',
      ...(['local_semantic', 'gliclass', 'jeff'].includes(String(params.engine ?? 'local_semantic'))
        ? [] : ['clef', 'clef-flash', 'openai-decisions'].includes(String(params.engine))
          ? ['include_confidence'] as const
          : ['include_confidence', 'include_justification'] as const),
    ],
    initialParams: { fields: [{ name: 'category', type: 'category', labels: [], description: '' }] },
  },
  'map.extract': {
    body: ExtractParamsBody,
    inlineDiagnosticFields: (params): readonly GeneratedFieldName<'map.extract'>[] => [
      'source', 'engine', 'fields', 'instruction', 'include_confidence', 'context',
      ...(isRecord(params.grounding) && params.grounding.enabled ? ['source_document_columns'] as const : []),
    ],
    initialParams: { fields: [{ name: 'value', type: 'text', description: '' }] },
    initialPromptParams: (prompt) => {
      const { name, type, description } = extractFieldFromQuestion(prompt);
      return { fields: [{ name, type, description }] };
    },
  },
  'map.mcp_extract': {
    body: declarativeParamsBody<'map.mcp_extract'>([
      'source', extractFields(), 'instruction',
      ({ params, setParams, errors }) => <ProjectMcpToolsSelector mcpServerIds={params.mcp_server_ids ?? []}
        error={errors.mcp_server_ids?.message}
        onMcpServerIdsChange={(mcp_server_ids) => setParams({ ...params, mcp_server_ids })} />,
      'model', 'include_confidence', 'context',
    ]),
    inlineDiagnosticFields: ['source', 'fields', 'instruction', 'mcp_server_ids', 'model',
      'include_confidence', 'context'],
    initialParams: { fields: [{ name: 'value', type: 'text', description: '' }], mcp_server_ids: [] },
  },
  'map.find': {
    body: declarativeParamsBody<'map.find'>(['source', 'instruction', extractFields(true), 'model']),
    defaultSheetName: 'Findings', inlineDiagnosticFields: ['source', 'instruction', 'fields', 'model'],
  },
  'map.translate': {
    body: TranslateParamsBody,
    inlineDiagnosticFields: ['source', 'engine', 'language'],
    initialParams: { engine: 'llm', target_language: 'English' },
    defaultOutputName: (key) => key === 'detected_language' ? 'translation_detected_language' : undefined,
  },
  'research.web_search': {
    fieldOrder: ['query', 'model', 'max_results'],
    outputLabel: (key) => key === 'answer' ? 'Answer' : 'Search results',
  },
  'research.extract': {
    body: declarativeParamsBody<'research.extract'>([
      'source', 'question', extractFields(), 'model', 'include_confidence', 'include_sources',
    ]),
    inlineDiagnosticFields: ['source', 'question', 'fields', 'model', 'include_confidence', 'include_sources'],
    initialParams: { fields: [{ name: 'value', type: 'text', description: '' }] },
    defaultOutputName: (key) => key === 'sources' ? 'answer_sources' : undefined,
  },
  'research.answer': {
    fieldOrder: ['source', 'question', 'model', 'include_sources'],
    defaultOutputName: (key) => key === 'sources' ? 'answer_sources' : undefined,
  },
  'reduce.group_summary': {
    fieldOrder: ['source', 'group_by', 'instruction', 'model'],
    defaultSheetName: 'Group summaries',
    fields: { instruction: PromptField },
  },
  'map.summarize': {
    fieldOrder: ['source', 'preset', 'instruction', 'model', 'context'],
    fields: { instruction: PromptField },
    outputLabel: (key) => key === 'summary' ? 'Summary' : undefined,
  },
  'map.columns_from_json': {
    fields: { routes: (props) => <ColumnsFromJsonRoutesField {...props} /> },
  },
  'map.clean_dates': {
    defaultOutputName: suffixedSourceOutput('cleaned', 'iso'),
    fields: {
      format: ({ value, onChange }) => (
        <CleanDatesForm format={typeof value === 'string' ? value : ''}
          onFormatChange={(format) => onChange(format.trim() ? format : null)} />
      ),
    },
  },
  'map.clean_column': { defaultOutputName: suffixedSourceOutput('cleaned', 'clean') },
  'resolve.substitute': {
    body: SubstituteForm satisfies GeneratedActionParamsBody<'resolve.substitute'>,
    primaryLabel: 'Apply mapping',
    primaryTestId: 'resolve-apply',
    defaultOutputName: suffixedSourceOutput('cleaned', 'clean'),
  },
  'resolve.replace': {
    body: ReplaceForm satisfies GeneratedActionParamsBody<'resolve.replace'>,
    primaryLabel: 'Apply rules',
    primaryTestId: 'resolve-apply',
    defaultOutputName: suffixedSourceOutput('cleaned', 'clean'),
  },
  'resolve.combine': {
    body: CombineForm satisfies GeneratedActionParamsBody<'resolve.combine'>,
    primaryLabel: 'Apply groups',
    primaryTestId: 'resolve-apply',
    defaultOutputName: suffixedSourceOutput('cleaned', 'clean'),
  },
  'resolve.fill_missing': {
    body: declarativeParamsBody<'resolve.fill_missing'>([
      'source',
      'method',
      { field: 'fill_value', when: ({ params }) => params.method === 'value', clearWhenHidden: true },
      'treat_blank_as_missing',
    ]),
    defaultOutputName: suffixedSourceOutput('cleaned', 'clean'),
  },
} satisfies ExactGeneratedActionCustomizationMap;
const CUSTOMIZATIONS: Record<string, StoredGeneratedActionCustomization> = EXACT_CUSTOMIZATIONS;

export function generatedActionCustomizationFor(
  actionId: string,
  pluginUI?: GeneratedActionCustomization,
): GeneratedActionCustomization | undefined {
  return pluginUI ?? CUSTOMIZATIONS[actionId] as GeneratedActionCustomization | undefined;
}
