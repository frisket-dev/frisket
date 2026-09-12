/* eslint-disable react-refresh/only-export-components -- sparse first-party field bindings */
import { Plus, Trash2 } from 'lucide-react';
import { useEffect, type ReactNode } from 'react';
import type { ActionFieldProps } from '../../generated/actionUI';

import type { CanonicalActionDraft, CanonicalActionParamValue } from '../../actions/canonicalActionDraft';
import type { SheetMeta } from '../../api/open';
import type { GeneratedActionParams } from '../../generated/actionTypes';
import { CleanDatesForm } from '../CleanDatesForm';
import { CombineForm } from '../resolve/CombineForm';
import { ReplaceForm } from '../resolve/ReplaceForm';
import { SubstituteForm } from '../resolve/SubstituteForm';
import { PythonCodeField } from './ActionParams';
import { PythonParamsBody } from './PythonParamsBody';
import { ApiCallParamsBody } from './ApiCallParamsBody';
import { TopicSectionsParamsBody } from './TopicSectionsParamsBody';
import { LinkTableParamsBody } from './LinkTableParamsBody';
import { JoinParamsBody } from './JoinParamsBody';
import { SemanticJoinParamsBody } from './SemanticJoinParamsBody';
import { MediaSegmentsParamsBody, TranscriptSegmentsParamsBody } from './TranscriptSegmentsParamsBody';
import { TemporalExtractParamsBody } from './TemporalExtractParamsBody';
import { DownloadMediaOptions } from './DownloadMediaOptions';
import { PdfTablesParamsBody } from './PdfTablesParamsBody';
import { ClusterParamsBody } from './ClusterParamsBody';
import { PageCaptureParamsBody } from './PageCaptureParamsBody';
import { NerParamsBody } from './NerParamsBody';
import { ClassifyParamsBody } from './ClassifyParamsBody';
import { ExtractParamsBody, FindParamsBody, McpExtractParamsBody } from './ExtractParamsBody';
import { TranslateParamsBody } from './TranslateParamsBody';
import { RECOMMENDED_SPACY_TYPES } from './nerLabelModel';
import { extractFieldFromQuestion } from './outputFieldModel';
import { FetchUrlParamsBody, VideoFramesParamsBody } from './MediaParamsBodies';
import { OcrParamsBody, TranscribeParamsBody } from './MediaReadParamsBodies';
import { ActionPromptField } from './ActionPromptField';
import { ListTableParamsBody } from './ListTableParamsBody';
import { EntityTableParamsBody } from './EntityTableParamsBody';
import { CensusGeographyField, CensusParamsBody, GeocodeParamsBody } from './GeospatialParamsBodies';
import type {
  DynamicGeneratedActionParamsBody,
  GeneratedActionParamsBody,
  GeneratedActionParamsBodyProps,
  EngineModelChoicePresentation,
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
  fieldOrder?: readonly string[];
  defaultOutputName?(key: string, draft: CanonicalActionDraft): string | undefined;
  outputLabel?(key: string): string | undefined;
  outputPrefix?: {
    label: string;
    initialValue(names?: Readonly<Record<string, string>>): string;
    outputName(key: string, prefix: string, outputs: readonly { key: string }[]): string;
    keys?: readonly string[];
    hint?: ReactNode;
  };
  primaryLabel?: string;
  primaryTestId?: string;
  freePublicApiLabel?: string;
  /** Fixed engines and provider-backed LLM models share one presentation while
   * retaining the established `{ engine, model }` request shape. */
  engineModelChoice?: EngineModelChoicePresentation;
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
  Omit<GeneratedActionCustomizationBase, 'fields' | 'fieldOrder' | 'initialParams'> & {
    initialParams?: Partial<GeneratedActionParams[K]>;
    fields?: Readonly<Partial<Record<GeneratedFieldName<K>,
      (props: GeneratedActionFieldProps) => ReactNode>>>;
    fieldOrder?: readonly GeneratedFieldName<K>[];
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
        <button type="button" className="mini-btn" data-testid="columns-from-json-add-route"
          onClick={() => onChange([...visible, { name: '', path: '' }])}>
          <Plus size={12} /> Add field
        </button>
      </div>
      <span className="form-hint">Use paths like $.address.city or $.items[0].name.</span>
      {visible.map((route, index) => (
        <div className="api-route-row" key={`route-${index}`}>
          <input className="form-input" aria-label={`Field ${index + 1} JSON path`}
            placeholder="$.path.to.value" value={route.path}
            data-testid={`columns-from-json-path-${index}`}
            onChange={(event) => update(index, 'path', event.target.value)} />
          <input className="form-input" aria-label={`Field ${index + 1} output name`}
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

function FillMissingParamsBody({ params, setParams, Field }:
  GeneratedActionParamsBodyProps<'resolve.fill_missing'>) {
  const usesExplicitValue = params.method === 'value';
  useEffect(() => {
    if (usesExplicitValue || !Object.prototype.hasOwnProperty.call(params, 'fill_value')) return;
    const rest = { ...params };
    delete rest.fill_value;
    setParams(rest);
  }, [params, setParams, usesExplicitValue]);
  return (
    <>
      <Field name="source" />
      <Field name="method" />
      {usesExplicitValue && <Field name="fill_value" />}
      <Field name="treat_blank_as_missing" />
    </>
  );
}

function PromptField({ label, value, onChange }: GeneratedActionFieldProps) {
  return <ActionPromptField label={label} value={typeof value === 'string' ? value : ''}
    rows={5} onChange={onChange} />;
}

function GuidanceField({ id, testid, label, value, onChange }: GeneratedActionFieldProps) {
  return <ActionPromptField id={id} testId={testid} label={label}
    value={typeof value === 'string' ? value : ''} rows={5} onChange={onChange} />;
}

const EXACT_CUSTOMIZATIONS = {
  'web.capture_page': { body: PageCaptureParamsBody, primaryLabel: 'Capture pages',
    defaultSheetName: 'Links', outputLabel: (key: string) => key === 'page' ? 'Save the snapshot to' : undefined },
  'cluster.values': { body: ClusterParamsBody, primaryLabel: 'Write canonical values',
    primaryTestId: 'cluster-commit-button', defaultOutputName: (_key, draft) =>
      typeof draft.source === 'string' ? `${draft.source}_canonical` : 'canonical' },
  'join.semantic': { body: SemanticJoinParamsBody, primaryLabel: 'Match rows', defaultSheetName: 'Matches',
    initialParams: { carry: [] } },
  'derive.join': { body: JoinParamsBody, primaryLabel: 'Join tables', defaultSheetName: 'Joined',
    outputLabel: (key: string) => key === '_merge' ? 'Indicator column name' : undefined },
  'media.ytdlp_download': {
    body: DownloadMediaOptions,
    primaryLabel: 'Download media',
    initialParams: { media_type: 'video' },
    defaultOutputName: (key: string) => key === 'audio' || key === 'video' ? 'media' : `media_${key}`,
  },
  'media.enclosure_materialize': { primaryLabel: 'Download enclosures' },
  'temporal.extract_range': { body: TemporalExtractParamsBody, primaryLabel: 'Extract range' },
  'derive.temporal_segments': { body: MediaSegmentsParamsBody, primaryLabel: 'Split into segments',
    defaultSheetName: 'Segments' },
  'media.fetch_url': { body: FetchUrlParamsBody, primaryLabel: 'Fetch URL' },
  'media.video_frames': { body: VideoFramesParamsBody, initialParams: { max_dimension: 720 },
    primaryLabel: 'Extract frames' },
  'media.extract_pdf_tables': { body: PdfTablesParamsBody, primaryLabel: 'Extract PDF tables',
    outputLabel: () => 'Result column name' },
  'derive.transcript_segments': { body: TranscriptSegmentsParamsBody, primaryLabel: 'Create transcript segments' },
  'derive.link_table': { body: LinkTableParamsBody, primaryLabel: 'Create link table' },
  'map.find_topic_sections': { body: TopicSectionsParamsBody },
  'map.api_call': { body: ApiCallParamsBody, primaryLabel: 'Run API calls', outputLabel: () => 'Save JSON response to' },
  'map.python': {
    body: PythonParamsBody,
    fields: {
      code: ({ label, value, onChange, testid }) => <PythonCodeField
        param={{ name: 'code', label, input: 'textarea' }}
        value={typeof value === 'string' ? value : ''} onChange={onChange} testid={testid} />,
    },
  },
  'enrich.geocode': { body: GeocodeParamsBody, freePublicApiLabel: 'Free public API request' },
  'enrich.census_demographics': {
    body: CensusParamsBody,
    fields: { geography: CensusGeographyField },
    freePublicApiLabel: 'US Census ACS enrichment',
  },
  'resolve.entities': {
    body: EntityTableParamsBody,
    primaryLabel: 'Create entity table',
    outputNamesReadOnly: true,
  },
  'media.extract_metadata': {
    outputPrefix: {
      label: 'Metadata',
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
    defaultOutputName: (key) => key === 'text' ? 'ocr_text' : key === 'blocks' ? 'ocr_text_boxes' : key === 'pdf' ? 'searchable_pdf' : undefined,
    hiddenOutputNameKeys: ['text', 'blocks'],
    outputLabel: (key) => key === 'pdf' ? 'Searchable PDF' : undefined,
    outputPrefix: {
      label: 'OCR result',
      initialValue: (names) => names?.text ?? 'ocr_text',
      outputName: (key, prefix) => key === 'text' ? prefix : `${prefix}_boxes`,
      keys: ['text', 'blocks'],
      hint: null,
    },
  },
  'media.transcribe': {
    body: TranscribeParamsBody,
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
    fieldOrder: ['source', 'question', 'model', 'context'],
    fields: { question: PromptField },
    outputLabel: (key) => key === 'answer' ? 'Save answer to' : undefined,
  },
  'map.ner': {
    body: NerParamsBody,
    fields: { extra_instructions: GuidanceField },
    initialParams: { labels: [...RECOMMENDED_SPACY_TYPES] },
    outputLabel: () => 'Save entities to',
    engineModelChoice: {
      engineParam: 'engine', modelParam: 'model', providerEngineId: 'llm',
      label: 'Entity recognition', fixedGroupLabel: 'Recognition engines',
    },
  },
  'map.classify': {
    body: ClassifyParamsBody,
    fields: { context: GuidanceField },
    initialParams: { fields: [{ name: 'category', type: 'category', labels: [], description: '' }] },
    engineModelChoice: {
      engineParam: 'engine', modelParam: 'model', providerEngineId: 'llm',
      label: 'Classifier', fixedGroupLabel: 'Classification engines',
    },
  },
  'map.extract': {
    body: ExtractParamsBody,
    fields: { instruction: GuidanceField, context: GuidanceField },
    initialParams: { fields: [{ name: 'value', type: 'text', description: '' }] },
    initialPromptParams: (prompt) => {
      const { name, type, description } = extractFieldFromQuestion(prompt);
      return { fields: [{ name, type, description }] };
    },
  },
  'map.mcp_extract': {
    body: McpExtractParamsBody,
    fields: { instruction: GuidanceField, context: GuidanceField },
    initialParams: {
      fields: [{ name: 'value', type: 'text', description: '' }],
      mcp_server_ids: [],
    },
  },
  'map.find': { body: FindParamsBody, defaultSheetName: 'Findings', fields: { instruction: GuidanceField } },
  'map.translate': {
    body: TranslateParamsBody,
    fields: { context: GuidanceField },
    initialParams: { engine: 'llm', target_language: 'English' },
    defaultOutputName: (key) => key === 'detected_language' ? 'translation_detected_language' : undefined,
    engineModelChoice: {
      engineParam: 'engine', modelParam: 'model', providerEngineId: 'llm',
      label: 'Translation engine', fixedGroupLabel: 'Translation engines',
    },
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
    outputLabel: (key) => key === 'summary' ? 'Save summary to' : undefined,
  },
  'map.columns_from_json': {
    fields: { routes: (props) => <ColumnsFromJsonRoutesField {...props} /> },
  },
  'map.clean_dates': {
    defaultOutputName: (key, draft) => key === 'cleaned' && typeof draft.source === 'string'
      ? `${draft.source}_iso` : undefined,
    fields: {
      format: ({ value, onChange }) => (
        <CleanDatesForm format={typeof value === 'string' ? value : ''}
          onFormatChange={(format) => onChange(format.trim() ? format : null)} />
      ),
    },
  },
  'map.clean_column': {
    defaultOutputName: (key, draft) => key === 'cleaned' && typeof draft.source === 'string'
      ? `${draft.source}_clean` : undefined,
  },
  'resolve.substitute': {
    body: SubstituteForm satisfies GeneratedActionParamsBody<'resolve.substitute'>,
    primaryLabel: 'Apply mapping',
    primaryTestId: 'resolve-apply',
    defaultOutputName: (key, draft) => key === 'cleaned' && typeof draft.source === 'string'
      ? `${draft.source}_clean` : undefined,
  },
  'resolve.replace': {
    body: ReplaceForm satisfies GeneratedActionParamsBody<'resolve.replace'>,
    primaryLabel: 'Apply rules',
    primaryTestId: 'resolve-apply',
    defaultOutputName: (key, draft) => key === 'cleaned' && typeof draft.source === 'string'
      ? `${draft.source}_clean` : undefined,
  },
  'resolve.combine': {
    body: CombineForm satisfies GeneratedActionParamsBody<'resolve.combine'>,
    primaryLabel: 'Apply groups',
    primaryTestId: 'resolve-apply',
    defaultOutputName: (key, draft) => key === 'cleaned' && typeof draft.source === 'string'
      ? `${draft.source}_clean` : undefined,
  },
  'resolve.fill_missing': {
    body: FillMissingParamsBody satisfies GeneratedActionParamsBody<'resolve.fill_missing'>,
    defaultOutputName: (key, draft) => key === 'cleaned' && typeof draft.source === 'string'
      ? `${draft.source}_clean`
      : undefined,
  },
} satisfies ExactGeneratedActionCustomizationMap;
const CUSTOMIZATIONS: Record<string, StoredGeneratedActionCustomization> = EXACT_CUSTOMIZATIONS;

export function generatedActionCustomizationFor(
  actionId: string,
  pluginUI?: GeneratedActionCustomization,
): GeneratedActionCustomization | undefined {
  return pluginUI ?? CUSTOMIZATIONS[actionId] as GeneratedActionCustomization | undefined;
}
