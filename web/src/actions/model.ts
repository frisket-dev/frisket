import type {
  ActionCatalogEntry,
  ActionCatalogFormParam,
  ActionCatalogPayload,
  ActionCatalogUiHints,
  JsonSchemaLike,
  ActionPlacement,
  LocalProviderCatalog,
  ModelOption,
  ActionParam,
  ActionSourceRequirement,
  ActionTemplate,
  ColumnDef,
  ColumnType,
  OutputField,
  RecipeFencePosture,
} from '../api/types';
import { hasNoActionDrawer } from './registry';
import { hasGeneratedActionForm } from '../api/types';
import { GENERATED_MODEL_OPTIONS } from './modelCatalog.generated';

/** Project authoring is a catalog fact, independent of the current sheet. */
export function isProjectScopedAction(entry: ActionCatalogEntry,
  createsSheet = entry.ui_hints?.typed_action?.creates_sheet === true): boolean {
  return entry.row_scope_policy?.kind === 'project'
    || (createsSheet && entry.row_scope_policy?.kind !== 'sheet_rows');
}

/** Trust copy must reflect the server-reported fence posture; unknown values fail closed. */
export const PYTHON_SNIPPET_TRUST_COPY: Record<
  RecipeFencePosture,
  { badge: string; detail: string; hint: string }
> = {
  enforced: {
    badge: '● confined by the kernel',
    detail:
      'Runs on this server, inside a kernel fence installed before your code: no network of any kind, and no reading or writing outside its own scratch directory, apart from the Python installation it imports from. Your project’s data and stored keys are refused, and provider keys are stripped from its environment. If the kernel cannot enforce that, the run is refused rather than run unconfined.',
    hint: 'The snippet sees `row` (a dict of this row’s values) and must set `result`. It runs on this server, inside a kernel fence: no network access, and it cannot read your project’s data or write outside its own scratch directory.',
  },
  partial: {
    badge: '▲ files not confined',
    detail:
      'Runs on this server with the server’s privileges. This server’s platform blocks the snippet’s network access, but does NOT confine the filesystem: it can read and write any file this server’s user can, stored keys included. Only run code you trust.',
    hint: 'The snippet sees `row` (a dict of this row’s values) and must set `result`. It runs on this server with the server’s privileges. Network access is blocked here, but the filesystem is not: it can read and write any file this server’s user can.',
  },
  none: {
    badge: '▲ not confined',
    detail:
      'Runs on this server with the server’s privileges: it can reach the network and read and write files outside the project. Provider keys are stripped from its environment, but not from disk. Only run code you trust.',
    hint: 'The snippet sees `row` (a dict of this row’s values) and must set `result`. It runs on this server with the server’s privileges: it can reach the network and read and write files outside the project. Provider keys are stripped from its environment, but not from disk.',
  },
  unknown: {
    badge: '▲ confinement unknown',
    detail:
      'This server has not said what confines a snippet, so assume nothing does: with the server’s privileges, it may reach the network and read and write files outside the project. Only run code you trust.',
    hint: 'The snippet sees `row` (a dict of this row’s values) and must set `result`. It runs on this server with the server’s privileges, and this server has not said what confines it — assume nothing does. Provider keys are stripped from its environment, but not from disk.',
  },
};

const RECIPE_FENCE_POSTURES = Object.keys(PYTHON_SNIPPET_TRUST_COPY) as RecipeFencePosture[];

/** Unrecognized posture values must never select reassuring copy. */
export function asRecipeFencePosture(value: unknown): RecipeFencePosture {
  return RECIPE_FENCE_POSTURES.includes(value as RecipeFencePosture)
    ? (value as RecipeFencePosture)
    : 'unknown';
}


export const MODEL_OPTIONS: ModelOption[] = GENERATED_MODEL_OPTIONS;

export const modelProviderId = (modelId: string): string => modelId.split('/', 1)[0];

const localProviderIdentity = (
  provider: LocalProviderCatalog['providers'][number],
): string => provider.kind === 'local_http' ? provider.endpoint_id : provider.id;

export function modelOptionsForTier(hosted: boolean): ModelOption[] {
  return hosted
    ? MODEL_OPTIONS.filter((option) => modelProviderId(option.id) !== 'ollama')
    : MODEL_OPTIONS;
}

export function defaultCopilotModel(catalog: LocalProviderCatalog | null): string {
  const byId = new Map(
    (catalog?.providers ?? []).map((provider) => [localProviderIdentity(provider), provider]),
  );
  const preferred: [string, string][] = [
    ['anthropic', 'anthropic/claude-sonnet-5'],
    ['openai', 'openai/gpt-5.6-terra'],
    ['gemini', 'gemini/gemini-3.6-flash'],
  ];
  for (const [provider, model] of preferred) {
    const entry = byId.get(provider);
    if (entry?.kind === 'platform_api' && entry.configured) return model;
  }
  const localServer = (catalog?.providers ?? []).find(
    (provider) => provider.kind === 'local_http' && provider.reachable && provider.models.length > 0,
  );
  if (localServer) return localServer.models[0].id;
  return 'anthropic/claude-sonnet-5';
}

export function providerIsUsable(provider: LocalProviderCatalog['providers'][number]): boolean {
  return provider.kind === 'local_http'
    ? Boolean(provider.reachable) && provider.models.length > 0
    : Boolean(provider.configured);
}

export function defaultActionModel(catalog: LocalProviderCatalog | null): string | null {
  if (!catalog) return null;
  const usableProviders = new Set(
    catalog.providers.filter(providerIsUsable).map(localProviderIdentity),
  );
  const staticMatch = MODEL_OPTIONS.find(
    (option) =>
      modelProviderId(option.id) !== 'ollama' &&
      usableProviders.has(modelProviderId(option.id)),
  );
  if (staticMatch) return staticMatch.id;
  const liveMatch = catalog.providers.find(
    (provider) => usableProviders.has(localProviderIdentity(provider)) && provider.models.length > 0,
  );
  return liveMatch?.models[0]?.id ?? null;
}

// Keep this missing-message fallback byte-identical to the server's cost-gate copy.

export const UNKNOWN_COST_GATE_MESSAGE =
  'Estimated cost is unknown (this run has no published price) — confirm to run it anyway.';

export const PROVIDER_LABELS: Record<string, string> = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  gemini: 'Gemini',
  openrouter: 'OpenRouter',
  ollama: 'Local server',
};

export function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}

export { formatUsd } from '../format';

export const actionFieldTypes: ColumnType[] = [
  'text', 'number', 'integer', 'boolean', 'category', 'json', 'list', 'date', 'link',
];

export const actionListItemFieldTypes = actionFieldTypes.filter((type) => type !== 'list');

export function singleInputRequirement(actionTemplate: ActionTemplate): ActionSourceRequirement | null {
  return actionTemplate.sourceRequirements?.find(
    (req) => req.mode === 'column' || req.mode === 'column_or_template'
      || (req.mode === 'columns' && req.max === 1),
  ) ?? null;
}

export function sourceRequirementForParam(
  actionTemplate: ActionTemplate,
  param: string,
  mode?: ActionSourceRequirement['mode'],
): ActionSourceRequirement | null {
  return actionTemplate.sourceRequirements?.find(
    (req) => req.param === param
      && (mode === undefined || req.mode === mode),
  ) ?? null;
}

export function primaryColumnRequirement(actionTemplate: ActionTemplate): ActionSourceRequirement | null {
  const direct = actionTemplate.sourceRequirements?.filter((requirement) => (
    requirement.mode !== 'template'
    && requirement.mode !== 'computed'
    && requirement.mode !== 'fixed_column'
  )) ?? [];
  return direct.find((requirement) => requirement.one_of_default)
    ?? direct[0]
    ?? null;
}

export function actionsForColumn(
  actionTemplates: ActionTemplate[],
  column: ColumnDef,
): ActionTemplate[] {
  const columnType = String(column.type);
  return actionTemplates.filter((template) => {
    const requirement = primaryColumnRequirement(template);
    const accepted = requirement?.accepted_column_types;
    if (!accepted?.length) return false;
    return accepted.map(String).includes(columnType);
  });
}

export function orderColumnActionsForNextStep(
  actions: ActionTemplate[],
  column: ColumnDef,
): ActionTemplate[] {
  const status = column.transcriptStatus;
  if (status !== 'missing' && status !== 'partial') return actions;
  const index = actions.findIndex((action) => action.kind === 'media.transcribe');
  if (index <= 0) return actions;
  const reordered = actions.slice();
  const [transcribeAction] = reordered.splice(index, 1);
  reordered.unshift(transcribeAction);
  return reordered;
}

export function compatibleSourceColumns(
  columns: ColumnDef[],
  requirement: ActionSourceRequirement | null,
): ColumnDef[] {
  const eligible = requirement?.ai_generated_only
    ? columns.filter((column) => Boolean(column.ai))
    : columns;
  if (!requirement?.accepted_column_types?.length) return eligible;
  const acceptedTypePriority = new Map(
    requirement.accepted_column_types.map((type, index) => [String(type), index]),
  );
  return eligible
    .map((column, index) => ({ column, index, typeRank: acceptedTypePriority.get(String(column.type)) }))
    .filter((entry): entry is { column: ColumnDef; index: number; typeRank: number } => (
      entry.typeRank !== undefined
    ))
    .sort((a, b) => a.typeRank - b.typeRank || a.index - b.index)
    .map((entry) => entry.column);
}

const GENERATED_PROMPTS: Record<string, string> = {
  'research.answer': 'Research this row and report what you find. Cite the sources you used.',
};

function titleizeLauncherActionName(name: string): string {
  return name
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function paramLabel(name: string): string {
  return name.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

function paramDefault(param: ActionCatalogFormParam): string | undefined {
  if (param.default === null || param.default === undefined) return undefined;
  return String(param.default);
}

function paramRequired(param: ActionCatalogFormParam): boolean {
  if (param.required !== undefined) return param.required;
  if (param.default !== undefined) return false;
  if (param.type === 'boolean') return false;
  return /\brequired\b/i.test(param.description ?? '');
}

function schemaStringMaxLength(schema: JsonSchemaLike | undefined): number | undefined {
  if (!schema) return undefined;
  const candidates = [
    schema,
    ...(Array.isArray(schema.anyOf)
      ? schema.anyOf.filter((value): value is JsonSchemaLike => (
          Boolean(value) && typeof value === 'object' && !Array.isArray(value)
        ))
      : []),
  ];
  for (const candidate of candidates) {
    const maxLength = candidate.maxLength;
    if (typeof maxLength === 'number' && Number.isInteger(maxLength) && maxLength >= 0) {
      return maxLength;
    }
  }
  return undefined;
}

function catalogFormParamToActionParam(
  param: ActionCatalogFormParam,
  schema?: JsonSchemaLike,
): ActionParam {
  const denseMeta = {
    ...(param.dense_group ? { denseGroup: param.dense_group } : {}),
    ...(param.dense_span ? { denseSpan: param.dense_span } : {}),
  };
  if (param.type === 'column' || param.type === 'column-optional' || param.type === 'columns') {
    return {
      name: param.name,
      label: param.label ?? paramLabel(param.name),
      input: param.type,
      defaultValue: paramDefault(param),
      required: paramRequired(param),
      hint: param.description,
      columnTypes: param.column_types,
      aiGeneratedOnly: param.ai_generated_only,
      ...denseMeta,
    };
  }
  if (param.type === 'category' && param.choices?.length) {
    const choiceOptions = param.choices.map((value) => ({
      value,
      label: param.choice_labels?.[value] ?? paramLabel(value),
    }));
    return {
      name: param.name,
      label: param.label ?? paramLabel(param.name),
      input: 'select',
      choices: param.choices,
      choiceOptions,
      defaultValue: paramDefault(param),
      required: paramRequired(param),
      hint: param.description,
      ...denseMeta,
    };
  }
  if (param.type === 'boolean') {
    return {
      name: param.name,
      label: param.label ?? paramLabel(param.name),
      input: 'checkbox',
      defaultValue: paramDefault(param) ?? 'false',
      required: paramRequired(param),
      hint: param.description,
      ...(param.layout === 'grid' ? { layout: 'grid' as const } : {}),
      ...(param.layout === 'grid' && param.group ? { group: param.group } : {}),
      ...(param.visible_when ? { visibleWhen: param.visible_when } : {}),
      ...denseMeta,
    };
  }
  const maxLength = schemaStringMaxLength(schema);
  return {
    name: param.name,
    label: param.label ?? paramLabel(param.name),
    input: param.type === 'textarea' ? 'textarea' : 'text',
    defaultValue: paramDefault(param),
    required: paramRequired(param),
    hint: param.description,
    ...(maxLength !== undefined ? { maxLength } : {}),
    ...(param.visible_when ? { visibleWhen: param.visible_when } : {}),
    ...denseMeta,
  };
}

function catalogLauncherHints(entry: ActionCatalogEntry): ActionCatalogUiHints {
  return entry.ui_hints ?? {};
}

function recordOutputField(raw: unknown, fallbackDescription: string): OutputField | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  const name = typeof record.name === 'string' ? record.name.trim() : '';
  if (!name) return null;
  const rawType = typeof record.type === 'string'
    ? record.type
    : typeof record.column_type === 'string'
      ? record.column_type
      : 'text';
  return {
    name,
    type: rawType as ColumnType,
    description: typeof record.description === 'string' ? record.description : fallbackDescription,
  };
}

type ResolvedOutputShape =
  | { status: 'fields'; fields: OutputField[] }
  | { status: 'no-columns' }
  | { status: 'unresolved'; missing: string };

function resolveOutputShapeFromCatalogEntry(entry: ActionCatalogEntry): ResolvedOutputShape {
  const hints = catalogLauncherHints(entry);
  if ((hints as Record<string, unknown>).output_table) return { status: 'no-columns' };
  const fallbackDescription = entry.description;
  const hintedOutputs = ((): unknown[] => {
    const raw = (hints as Record<string, unknown>).default_outputs ??
      (hints as Record<string, unknown>).default_output_fields;
    return Array.isArray(raw) ? raw : [];
  })();
  const fromHints = hintedOutputs.flatMap((raw) => {
    const field = recordOutputField(raw, fallbackDescription);
    return field ? [field] : [];
  });
  if (fromHints.length) return { status: 'fields', fields: fromHints };

  if (hints.default_output?.name) {
    return {
      status: 'fields',
      fields: [{
        name: hints.default_output.name,
        type: hints.default_output.type ?? 'text',
        description: hints.default_output.description ?? fallbackDescription,
      }],
    };
  }

  return {
    status: 'unresolved',
    missing: 'the catalog action declares no default output columns',
  };
}

function usableCatalogFormParam(param: ActionCatalogFormParam): boolean {
  return typeof param.name === 'string' && param.name.trim().length > 0;
}

export function generatedActionTemplateFromCatalogEntry(entry: ActionCatalogEntry): ActionTemplate | null {
  // Malformed declarations cannot create a template. The complete-catalog
  // presentation validator owns their refusal and diagnostic.
  if (!entry?.ui_hints || !entry.input_schema) return null;
  if (hasNoActionDrawer(entry.kind)) return null;
  if (!hasGeneratedActionForm(entry)) return null;
  const kind = entry.kind;
  const hints = catalogLauncherHints(entry);
  const shape = resolveOutputShapeFromCatalogEntry(entry);
  const usesModel = hints.uses_model ?? entry.required_capabilities.includes('model:complete');
  const prompt = GENERATED_PROMPTS[kind] ?? entry.description;
  const outputShapeError = shape.status === 'unresolved'
    ? { actionKind: entry.kind, missing: shape.missing }
    : undefined;
  const defaultFields = shape.status === 'fields' ? shape.fields : [];
  const template: ActionTemplate = {
    kind,
    actionKind: entry.kind,
    authoringContractVersion: entry.authoring_contract_version,
    actionTitle: entry.title,
    actionDescription: entry.description,
    actionCategory: typeof hints.category === 'string' ? hints.category : undefined,
    generatedAction: hasGeneratedActionForm(entry) ? true : undefined,
    name: entry.title || titleizeLauncherActionName(kind),
    description: entry.description,
    defaultPrompt: usesModel ? prompt : '',
    defaultFields,
    llm: usesModel,
    produces: entry.ui_hints.typed_action?.creates_sheet || shape.status === 'no-columns' ? 'sheet' : undefined,
    noPrompt: !usesModel,
    noFields: shape.status !== 'fields',
    outputShapeError,
    requiresConfirmation: entry.cost_policy?.requires_confirmation === true,
    externalMetered: entry.cost_policy?.kind === 'external_metered',
    params: catalogFormParamsForActionTemplate(entry),
    sourceRequirements: hints.source_requirements,
    engines: hints.engines?.length ? hints.engines : undefined,
    pricing: hints.pricing,
    pricingOptions: hints.pricing_options,
    costSource: hints.cost_source,
    costSourceOptions: hints.cost_source_options,
    missingCredentials: hints.missing_credentials?.length ? hints.missing_credentials : undefined,
  };
  return template;
}

function catalogFormParamsForActionTemplate(entry: ActionCatalogEntry): ActionParam[] | undefined {
  const hints = catalogLauncherHints(entry);
  if (hasGeneratedActionForm(entry)) return generatedCatalogParams(entry);
  const hasDedicatedEngineSelector = Boolean(hints.engines?.length);
  const params = hints.form_params?.filter((param) => (
    usableCatalogFormParam(param) &&
    (!hasDedicatedEngineSelector || param.name !== 'engine')
  ));
  if (!params?.length) return undefined;
  const validators = hints.param_validators ?? {};
  return params.map((param) => {
    const actionParam = catalogFormParamToActionParam(
      param,
      entry.input_schema.properties?.[param.name],
    );
    const validator = validators[param.name];
    return validator ? { ...actionParam, validator } : actionParam;
  });
}

const CLEAN_COLUMN_LABELS: Readonly<Record<string, string>> = {
  source: 'Column to clean',
  case: 'Case',
  blank_null_tokens: 'Blank null tokens',
  lowercase_emails: 'Lowercase emails',
  normalize_us_phone: 'Normalize US phone',
  expand_abbreviations: 'Expand abbreviations',
  reorder_person_name: 'Reorder Last, First',
  canonicalize_duplicates: 'Merge duplicate variants',
  strip_edge_punct: 'Strip edge punctuation',
  normalize_unicode_punct: 'Normalize quotes & dashes',
  remove_thousands_separators: 'Remove thousands separators',
  remove_all_commas: 'Remove all commas',
  make_numeric: 'Make numeric',
  null_tokens: 'Blank these tokens',
};

const CLEAN_COLUMN_CASE_LABELS: Readonly<Record<string, string>> = {
  keep: 'Keep as-is',
  smart_title: 'Fix capitalization (names & titles)',
  title: 'Title Case',
  upper: 'UPPERCASE',
  lower: 'lowercase',
};

const METADATA_OUTPUT_MODE_LABELS: Readonly<Record<string, string>> = {
  columns: 'Multiple columns',
  object: 'One column',
};

function generatedCatalogParams(entry: ActionCatalogEntry): ActionParam[] | undefined {
  const properties = Object.entries(entry.input_schema.properties ?? {});
  if (!properties.length) return undefined;
  const required = new Set(entry.input_schema.required ?? []);
  const controls = entry.ui_hints.semantic_controls as Record<string, unknown> | undefined;
  const requirements = entry.ui_hints.source_requirements ?? [];
  const presentations = new Map(
    (entry.ui_hints.form_params ?? []).map((param) => [param.name, param]),
  );
  return properties.map(([name, schema]) => {
    const semantic = controls?.[name];
    const requirement = requirements.find((candidate) => candidate.param === name);
    const presentation = presentations.get(name);
    const choices = Array.isArray(schema.enum)
      ? schema.enum.filter((value): value is string => typeof value === 'string')
      : [];
    const cleanColumn = entry.kind === 'map.clean_column';
    const type = semantic === 'column'
      ? 'column'
      : semantic === 'columns'
        ? 'columns'
        : semantic === 'rich_source' || semantic === 'model'
          ? 'text'
        : semantic === 'template'
          ? 'textarea'
          : choices.length
            ? 'category'
          : schema.type === 'boolean'
            ? 'boolean'
            : 'text';
    const declaration: ActionCatalogFormParam = {
      name,
      type,
      label: cleanColumn ? CLEAN_COLUMN_LABELS[name] : presentation?.label ?? requirement?.label
        ?? (typeof schema.title === 'string' ? schema.title : undefined),
      required: required.has(name),
      default: schema.default as ActionCatalogFormParam['default'],
      description: presentation?.description ?? schema.description,
      column_types: requirement?.accepted_column_types,
      ai_generated_only: requirement?.ai_generated_only,
      visible_when: presentation?.visible_when,
      ...(choices.length ? { choices,
        choice_labels: cleanColumn && name === 'case' ? CLEAN_COLUMN_CASE_LABELS
          : entry.kind === 'media.extract_metadata' && name === 'output_mode'
            ? METADATA_OUTPUT_MODE_LABELS
            : undefined,
      } : {}),
      ...(cleanColumn && type === 'boolean' ? { layout: 'grid', group: 'Cleanings' } : {}),
      ...(cleanColumn && name === 'null_tokens' ? {
        visible_when: { param: 'blank_null_tokens', value: 'true' },
      } : {}),
    };
    const param = catalogFormParamToActionParam(declaration, schema);
    return { ...param, ...(semantic === 'template' ? { monospace: true } : {}) };
  });
}

export const ACTION_PLACEMENTS: Partial<Record<string, ActionPlacement>> = {
  'media.ytdlp_download': { tab: 'data', group: 'WEB', order: 0, primary: true },
  'media.fetch_url': { tab: 'data', group: 'WEB', order: 1 },
  'web.capture_page': { tab: 'data', group: 'WEB', order: 2 },
  'web.capture_screenshot': { tab: 'data', group: 'WEB', order: 3 },
  'media.enclosure_materialize': { tab: 'data', group: 'WEB', order: 4 },
  'media.video_frames': { tab: 'media', group: 'VIDEO', order: 0, primary: true },
  'derive.temporal_segments': { tab: 'media', group: 'VIDEO', order: 1 },
  'map.find_visual_cuts': { tab: 'media', group: 'VIDEO', order: 2 },
  'temporal.extract_range': { tab: 'media', group: 'VIDEO', order: 3 },
  'media.extract_faces': { tab: 'media', group: 'VIDEO', order: 4 },
  'media.transcribe': { tab: 'media', group: 'TRANSCRIPTS', order: 0, primary: true },
  'map.find_topic_sections': { tab: 'media', group: 'TRANSCRIPTS', order: 1 },
  'derive.transcript_segments': { tab: 'media', group: 'TRANSCRIPTS', order: 2 },
  'media.ocr': { tab: 'media', group: 'DOCUMENTS', order: 0, primary: true },
  'media.to_markdown': { tab: 'media', group: 'DOCUMENTS', order: 1 },
  'media.extract_pdf_tables': { tab: 'media', group: 'DOCUMENTS', order: 2 },
  'media.extract_metadata': { tab: 'media', group: 'DOCUMENTS', order: 3 },
  'derive.table_from_list': { tab: 'tables', group: 'TABLES', order: 0, primary: true },
  'map.columns_from_json': { tab: 'tables', group: 'TABLES', order: 1 },
  'join.semantic': { tab: 'tables', group: 'TABLES', order: 2 },
  'derive.join': { tab: 'tables', group: 'TABLES', order: 3 },
  'derive.link_table': { tab: 'tables', group: 'TABLES', order: 4 },
  'map.ask': { tab: 'analyze', group: 'ANALYZE', order: 0, primary: true },
  'map.summarize': { tab: 'analyze', group: 'ANALYZE', order: 1 },
  'reduce.group_summary': { tab: 'analyze', group: 'ANALYZE', order: 2 },
  'map.classify': { tab: 'analyze', group: 'ANALYZE', order: 3 },
  'map.judge': { tab: 'analyze', group: 'ANALYZE', order: 4 },
  'map.extract': { tab: 'analyze', group: 'EXTRACT', order: 0, primary: true },
  'map.mcp_extract': { tab: 'analyze', group: 'EXTRACT', order: 1 },
  'map.ner': { tab: 'analyze', group: 'EXTRACT', order: 2 },
  'map.regex_extract': { tab: 'analyze', group: 'EXTRACT', order: 3 },
  'map.find': { tab: 'analyze', group: 'EXTRACT', order: 4 },
  'map.clean_column': { tab: 'transform', group: 'TRANSFORM', order: 0, primary: true },
  'map.clean_dates': { tab: 'transform', group: 'TRANSFORM', order: 1 },
  'map.template': { tab: 'transform', group: 'TRANSFORM', order: 2 },
  'map.python': { tab: 'transform', group: 'TRANSFORM', order: 3 },
  'cluster.values': { tab: 'transform', group: 'RESOLVE', order: 0, primary: true },
  'resolve.substitute': { tab: 'transform', group: 'RESOLVE', order: 1 },
  'resolve.replace': { tab: 'transform', group: 'RESOLVE', order: 2 },
  'resolve.combine': { tab: 'transform', group: 'RESOLVE', order: 3 },
  'resolve.fill_missing': { tab: 'transform', group: 'RESOLVE', order: 4 },
  'map.translate': { tab: 'transform', group: 'LANGUAGE', order: 0, primary: true },
  'frisket.transliterate.transliterate': { tab: 'transform', group: 'LANGUAGE', order: 1 },
  'research.web_search': { tab: 'research', group: 'RESEARCH', order: 0, primary: true },
  'research.answer': { tab: 'research', group: 'RESEARCH', order: 1 },
  'map.api_call': { tab: 'research', group: 'RESEARCH', order: 2 },
  'enrich.geocode': { tab: 'research', group: 'LOCATION', order: 0, primary: true },
  'enrich.census_demographics': { tab: 'research', group: 'LOCATION', order: 1 },
  'map.to_geo_point': { tab: 'research', group: 'LOCATION', order: 2 },
};

/** Host-owned discovery vocabulary for catalog-generated built-ins. This is
 * presentation metadata only: canonical Params and request construction stay
 * entirely catalog/host driven. */
const ACTION_SEARCH_KEYWORDS: Partial<Record<string, string[]>> = {
  'research.web_search': ['web search'],
  'resolve.substitute': ['substitute', 'recode', 'map values', 'lookup', 'resolve', 'clean'],
  'resolve.replace': ['replace', 'rules', 'find and replace', 'regex', 'resolve', 'clean'],
  'resolve.combine': ['combine', 'group values', 'buckets', 'merge', 'resolve', 'clean'],
  'resolve.fill_missing': ['fill', 'missing', 'blank', 'null', 'ffill', 'impute', 'resolve'],
};

const GENERATED_CATEGORY_PLACEMENTS: Record<string, ActionPlacement> = {
  cleanup: { tab: 'transform', group: 'TRANSFORM', order: 100 },
  convert: { tab: 'transform', group: 'TRANSFORM', order: 100 },
  text: { tab: 'analyze', group: 'ANALYZE', order: 100 },
  extract: { tab: 'analyze', group: 'EXTRACT', order: 100 },
};

function applyActionPlacements(templates: ActionTemplate[]): ActionTemplate[] {
  return templates.map((template) => {
    const hostPlacement = ACTION_PLACEMENTS[template.kind];
    const categoryPlacement = template.generatedAction && template.actionCategory
      ? GENERATED_CATEGORY_PLACEMENTS[template.actionCategory]
      : undefined;
    return {
      ...template,
      keywords: ACTION_SEARCH_KEYWORDS[template.kind] ?? template.keywords,
      placement: hostPlacement ?? categoryPlacement,
    };
  });
}

export function actionTemplatesFromCatalog(catalog: ActionCatalogPayload): ActionTemplate[] {
  return applyActionPlacements(catalog.actions.flatMap((entry) => {
    const template = generatedActionTemplateFromCatalogEntry(entry);
    return template ? [template] : [];
  }));
}
