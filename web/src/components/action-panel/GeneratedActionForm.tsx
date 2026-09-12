import { createContext, Fragment, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode } from 'react';
import { Eye } from 'lucide-react';
import { PluginActionUIBoundary } from './PluginActionUIBoundary';
import type { GeneratedActionCustomization } from './generatedActionCustomizations';

import { compatibleSourceColumns, defaultActionModel, formatUsd, isProjectScopedAction } from '../../actions/model';
import { quotedUsd } from '../../actions/quotedCost';
import { formatDuration } from '../../format';
import { parseStrictJson } from '../../actions/strictJson';
import { listProviders } from '../../api/open';
import type { ActionParamResolution, ActionTemplate, GeneratedActionCatalogEntry,
  GeneratedActionDraft, GeneratedActionRequest, PreviewSampleResult, SheetMeta } from '../../api/types';
import type { RunEstimate } from '../../api/types';
import { freshActionRequestKey } from '../../api/types';
import { ActionFormHeader } from './ActionFormHeader';
import { ActionCredentialGate } from './ActionCredentialGate';
import { orderedSourceColumns } from './ActionSourceSection';
import type { ActionParamFieldPresentationProps } from './ActionParams';
import {
  buildCanonicalDraft,
  buildCanonicalDraftFromValidatedParams,
  GenericSchemaRenderer,
  serializeCanonicalDraft,
  setCanonicalDraftField,
  type CanonicalDraft,
  type CanonicalFieldValue,
} from './actionPresentation';
import { RunScopeFooter } from './RunScopeFooter';
import {
  SourceInputControl,
  TemplateComposer,
  type SourceInputMode,
} from './SourceInputControl';
import { OutputNameCombobox } from './TargetSaveToControl';
import { dedupeDefaultColumnName, existingColumnByName } from './formControlHelpers';
import { generatedActionCustomizationFor } from './generatedActionCustomizations';
import pickerStyles from './EngineModelChoice.module.css';
import { ModelPicker } from '../ModelPicker';
import { EnginePicker } from '../EnginePicker';
import { JoinOutputNames } from './JoinOutputNames';
import { PdfTablesReview, type PdfTablesMaterializeIntent, type PdfTablesExportIntent } from './PdfTablesReview';
import type { OcrCompareTarget } from '../../actions/ocrCompare';

interface RichSourceEditorState {
  mode: SourceInputMode;
  columns: string[];
  template: string;
}
function templateText(value: CanonicalFieldValue | undefined): string {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && typeof value.text === 'string' ? value.text : '';
}
const EMPTY_ROW_IDS: string[] = [];

function jsonFieldText(value: CanonicalFieldValue | undefined): string {
  return typeof value === 'string' ? value
    : value === undefined ? '' : JSON.stringify(value, null, 2);
}

function JsonSchemaField({ field, value, schemaType, onChange }: {
  field: ActionParamFieldPresentationProps;
  value: CanonicalFieldValue | undefined;
  schemaType: 'object' | 'array';
  onChange(value: CanonicalFieldValue): void;
}) {
  const [text, setText] = useState(() => jsonFieldText(value));
  const lastValue = useRef(value);
  useEffect(() => {
    if (value === lastValue.current) return;
    lastValue.current = value;
    setText(jsonFieldText(value));
  }, [value]);
  return <div className="param-row">
    <label className="form-label" htmlFor={field.id}>{field.label}</label>
    <textarea id={field.id} className="form-input" rows={7} data-testid={field.testid}
      value={text} aria-invalid={typeof value === 'string'} onChange={(event) => {
        const raw = event.target.value;
        setText(raw);
        let next: CanonicalFieldValue = raw;
        try {
          const parsed = parseStrictJson(raw);
          if (schemaType === 'array' ? Array.isArray(parsed)
            : parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
            next = parsed as CanonicalFieldValue;
          }
        } catch { /* Keep unfinished text editable; the host blocks invalid Params. */ }
        lastValue.current = next;
        onChange(next);
      }} />
    {typeof value === 'string' && <p className="form-error" role="alert">
      Enter a valid JSON {schemaType}.
    </p>}
  </div>;
}

function NumericSchemaField({ field, value, onChange }: {
  field: ActionParamFieldPresentationProps;
  value: CanonicalFieldValue | undefined;
  onChange(value: string): void;
}) {
  const [text, setText] = useState(field.value);
  const lastValue = useRef(value);
  // Keep intermediate text ("0.", "0.70", "1e-") while publishing the
  // numeric value to Params. External edits still replace the display.
  useEffect(() => {
    if (value === lastValue.current) return;
    lastValue.current = value;
    setText(field.value);
  }, [value, field.value]);
  return <div className="param-row">
    <label className="form-label" htmlFor={field.id}>{field.label}</label>
    <input id={field.id} className="form-input" inputMode="decimal"
      data-testid={field.testid} value={text} onChange={(event) => {
        const raw = event.target.value;
        setText(raw);
        const numeric = raw.trim() === '' ? raw : Number(raw);
        lastValue.current = typeof numeric === 'number' && Number.isFinite(numeric)
          ? numeric : raw;
        onChange(raw);
      }} />
  </div>;
}

interface GeneratedFieldContextValue {
  actionTemplate: ActionTemplate;
  draft: CanonicalDraft;
  columns: SheetMeta['columns'];
  diagnostics: ActionParamResolution['diagnostics'];
  renderField(field: ActionParamFieldPresentationProps): ReactNode | undefined;
  onFieldChange(name: string, value: CanonicalFieldValue): void;
}
const GeneratedFieldContext = createContext<GeneratedFieldContextValue | null>(null);

function GeneratedField({ name, testId, label }: { name: string; testId?: string; label?: string }) {
  const context = useContext(GeneratedFieldContext);
  if (!context) throw new Error('GeneratedField must be rendered by GeneratedActionForm');
  const param = context.actionTemplate.params?.find((candidate) => candidate.name === name);
  if (!param) throw new Error(`Unknown canonical action field: ${name}`);
  return <GenericSchemaRenderer
    template={{ ...context.actionTemplate, params: [{ ...param,
      label: label ?? param.label, visibleWhen: undefined }] }}
    draft={context.draft} columns={context.columns} diagnostics={context.diagnostics}
    renderField={context.renderField} onFieldChange={context.onFieldChange}
    testIdOverrides={testId ? { [name]: testId } : undefined} />;
}

export interface GeneratedActionFormProps {
  projectId?: string | null;
  catalogEntry: GeneratedActionCatalogEntry;
  actionTemplate: ActionTemplate;
  /** Saved/Copilot launches may give this run a more useful title than the
   * catalog's generic action name. */
  title?: string;
  sheet: SheetMeta | null;
  selectedRowIds?: string[];
  hasExactRowScopeInitializer?: boolean;
  initialSourceColumn?: string;
  initialPrompt?: string;
  initialDraft?: GeneratedActionDraft;
  /** Keep the accepted drawer mounted through catalog refresh without letting
   * its stale snapshot authorize new work. */
  catalogAccepted?: boolean;
  running: boolean;
  runningLabel?: string;
  switchActions?: ActionTemplate[];
  onSwitchAction?(actionTemplate: ActionTemplate): void;
  onNavigateToAction?(actionKind: string, sourceColumn?: string): void;
  sampleColumnValues?(columnId: string): string[];
  onOpenDiagnose?(): void;
  onBackfill?: (targetColumnName: string) => void;
  onMaterializePdfTables?(intent: PdfTablesMaterializeIntent): void;
  onExportPdfTables?(intent: PdfTablesExportIntent): void;
  onOpenOcrCompare?(target: OcrCompareTarget): void;
  resolveParams(
    request: Pick<GeneratedActionRequest, 'action_id' | 'scope' | 'params'>,
  ): Promise<ActionParamResolution>;
  estimateAction?(request: GeneratedActionRequest): Promise<RunEstimate>;
  onExecute(
    request: GeneratedActionRequest,
    intent: 'preview' | 'run',
    onPreviewComplete?: (result: PreviewSampleResult) => void,
  ): void;
  onClose(): void;
}
function defaultColumn(entry: GeneratedActionCatalogEntry, param: string, columns: SheetMeta['columns'],
  sourceColumn: string | undefined, used: ReadonlySet<string>): string {
  const requirement = entry.ui_hints.source_requirements
    ?.find((candidate) => candidate.param === param) ?? null;
  const eligible = compatibleSourceColumns(orderedSourceColumns(columns), requirement);
  const candidates = eligible.some((column) => !used.has(column.name))
    ? eligible.filter((column) => !used.has(column.name)) : eligible;
  const normalize = (value: string) => value.toLowerCase().replace(/[^a-z0-9]/g, '');
  const key = normalize(param);
  const aliases = key.includes('lat') ? ['latitude', 'lat', 'y']
    : key.includes('lon') || key.includes('lng') ? ['longitude', 'lon', 'lng', 'long', 'x'] : [];
  const exact = candidates.find((column) => aliases.includes(normalize(column.name)));
  const partial = candidates.find((column) => aliases.slice(0, -1)
    .some((alias) => normalize(column.name).includes(alias)));
  if (exact || partial) return (exact ?? partial)!.name;
  if (sourceColumn && candidates.some((column) => column.name === sourceColumn)) {
    return sourceColumn;
  }
  return candidates[0]?.name ?? '';
}

function initialParams(entry: GeneratedActionCatalogEntry, template: ActionTemplate, columns: SheetMeta['columns'],
  sourceColumn?: string, saved?: GeneratedActionDraft, prompt?: string): CanonicalDraft {
  const savedParams = saved ? buildCanonicalDraftFromValidatedParams(entry, saved.params) : {};
  // A saved request owns its Params, including deliberately omitted
  // optional values. Do not manufacture text defaults for structured fields.
  if (saved) {
    return buildCanonicalDraft(template, savedParams);
  }
  const usedColumns = new Set(Object.entries(entry.ui_hints.semantic_controls).flatMap(
    ([name, control]) => control === 'column' && typeof savedParams[name] === 'string'
      ? [savedParams[name] as string]
      : [],
  ));
  const edits: CanonicalDraft = {};
  for (const param of template.params ?? []) {
    const savedValue = savedParams[param.name];
    if (savedValue !== undefined) {
      edits[param.name] = savedValue;
      continue;
    }
    const schema = entry.input_schema.properties?.[param.name];
    const control = entry.ui_hints.semantic_controls[param.name];
    if (control === 'column' || control === 'column_or_template') {
      const value = defaultColumn(entry, param.name, columns, sourceColumn, usedColumns);
      edits[param.name] = value;
      if (value) usedColumns.add(value);
    } else if (control === 'columns' && sourceColumn) {
      const requirement = entry.ui_hints.source_requirements
        ?.find((candidate) => candidate.param === param.name) ?? null;
      edits[param.name] = compatibleSourceColumns(columns, requirement)
        .some((column) => column.name === sourceColumn) ? [sourceColumn] : [];
    } else if (control === 'rich_source') {
      const requirement = entry.ui_hints.source_requirements
        ?.find((candidate) => candidate.param === param.name) ?? null;
      const eligible = compatibleSourceColumns(orderedSourceColumns(columns), requirement);
      edits[param.name] = sourceColumn
        && eligible.some((column) => column.name === sourceColumn)
        ? [sourceColumn]
        : eligible.slice(0, Math.max(1, requirement?.min ?? 1)).map((column) => column.name);
    } else if (control === 'template') {
      edits[param.name] = schema && 'default' in schema
        ? schema.default as CanonicalFieldValue
        : { text: sourceColumn ? `{{${sourceColumn}}}` : '' };
    } else edits[param.name] = schema && 'default' in schema
      ? schema.default as CanonicalFieldValue
      : control === 'columns' || schema?.type === 'array' ? [] : '';
  }
  const customization = generatedActionCustomizationFor(entry.kind);
  const promptField = template.params?.find((param) => param.name === 'question' || param.name === 'instruction');
  const promptParams = prompt ? customization?.initialPromptParams?.(prompt)
    ?? (promptField ? { [promptField.name]: entry.ui_hints.semantic_controls[promptField.name] === 'template'
      ? { text: prompt } : prompt } : {}) : {};
  return buildCanonicalDraft(template, {
    ...edits,
    ...customization?.initialParams,
    ...promptParams,
  });
}

function initialOutputNames(entry: GeneratedActionCatalogEntry, columns: SheetMeta['columns'],
  draft: CanonicalDraft, saved?: GeneratedActionDraft): Record<string, string> {
  if (entry.ui_hints.typed_action?.creates_sheet === true) return { ...saved?.output_names };
  if (entry.ui_hints.dynamic_outputs === true) return { ...saved?.output_names };
  const customization = generatedActionCustomizationFor(entry.kind);
  const prefix = customization?.outputPrefix;
  const linkedPrefix = prefix?.keys?.length ? prefix : undefined;
  const defaultPrefix = linkedPrefix
    ? dedupeOutputPrefix(linkedPrefix, columns, entry.ui_hints.logical_outputs) : undefined;
  return Object.fromEntries(entry.ui_hints.logical_outputs.map(({ key }) => [key,
    saved ? saved.output_names[key] ?? key
      : defaultPrefix && linkedPrefix?.keys?.includes(key)
        ? linkedPrefix.outputName(key, defaultPrefix, entry.ui_hints.logical_outputs)
        : dedupeDefaultColumnName(customization?.defaultOutputName?.(key, draft) ?? key, columns),
  ]));
}

function dedupeOutputPrefix(
  prefix: NonNullable<GeneratedActionCustomization['outputPrefix']>,
  columns: SheetMeta['columns'],
  outputs: readonly { key: string }[],
): string {
  const keys = prefix.keys!;
  const base = prefix.initialValue();
  const collides = (candidate: string) => keys.some((key) => Boolean(
    existingColumnByName(columns, prefix.outputName(key, candidate, outputs)),
  ));
  if (!collides(base)) return base;
  for (let suffix = 2; suffix <= columns.length + 1; suffix += 1) {
    const candidate = `${base}_${suffix}`;
    if (!collides(candidate)) return candidate;
  }
  return `${base}_${columns.length + 2}`;
}

function automaticOutputNameCollision(
  key: string,
  draft: CanonicalDraft,
  columns: SheetMeta['columns'],
  customization: GeneratedActionCustomization | undefined,
): { original: string; renamed: string } | null {
  const original = customization?.defaultOutputName?.(key, draft) ?? key;
  const renamed = dedupeDefaultColumnName(original, columns);
  return renamed === original ? null : { original, renamed };
}

function validateOutputNames(
  entry: GeneratedActionCatalogEntry,
  outputs: readonly { key: string }[],
  names: Readonly<Record<string, string>>,
  createsSheet: boolean,
): string | null {
  const expectedKeys = outputs.map(({ key }) => key);
  const submittedKeys = Object.keys(names);
  if (createsSheet) {
    if (expectedKeys.length && submittedKeys.some((key) => !expectedKeys.includes(key))) {
      return `Output names do not match ${entry.kind}.`;
    }
    const final = expectedKeys.length ? expectedKeys.map((key) => names[key]?.trim() ?? key)
      : Object.values(names).map((name) => name.trim());
    if (final.some((name) => !name)) return 'Output column names must not be blank.';
    return new Set(final.map((name) => name.toLowerCase())).size === final.length
      ? null : 'Output column names must be unique.';
  }
  if (submittedKeys.length !== expectedKeys.length
    || submittedKeys.some((key) => !expectedKeys.includes(key))) {
    return `Output names do not match ${entry.kind}.`;
  }
  const finalNames = expectedKeys.map((key) => names[key]?.trim() ?? '');
  if (finalNames.some((name) => !name)) return 'Name every output column.';
  if (new Set(finalNames.map((name) => name.toLowerCase())).size !== finalNames.length) {
    return 'Output column names must be unique.';
  }
  return null;
}

function compatibleOutputColumns(
  columns: SheetMeta['columns'],
  columnType: string | null,
  existingColumnPolicy: 'generated' | 'compatible' = 'generated',
): SheetMeta['columns'] {
  return columns.filter((column) => (
    (existingColumnPolicy === 'compatible'
      || (column.ai !== undefined && column.generationManaged === true))
    && (columnType === null || column.type === columnType)
  ));
}

const EMPTY_COLUMNS: SheetMeta['columns'] = [];

export function GeneratedActionForm(props: GeneratedActionFormProps) {
  if (!props.sheet && !isProjectScopedAction(props.catalogEntry)) {
    return <p role="alert">Choose a sheet before opening this action.</p>;
  }
  const ui = props.catalogEntry.ui_hints.action_ui;
  if (ui) {
    if (!props.projectId) return <p role="alert">Choose a project before opening this action.</p>;
    return <PluginActionUIBoundary key={JSON.stringify([props.projectId, ui.plugin_id, props.catalogEntry.kind, ui.package_sha256, ui.export_name])}
      entry={props.catalogEntry} projectId={props.projectId}>
      {(pluginUI) => <GeneratedActionFormContents {...props} pluginUI={pluginUI} />}
    </PluginActionUIBoundary>;
  }
  return <GeneratedActionFormContents {...props} />;
}

function GeneratedActionFormContents({
  pluginUI,
  catalogEntry,
  actionTemplate,
  title,
  sheet,
  selectedRowIds = EMPTY_ROW_IDS,
  hasExactRowScopeInitializer = false,
  initialSourceColumn,
  initialPrompt,
  initialDraft,
  catalogAccepted = true,
  running,
  runningLabel = 'Running…',
  switchActions,
  onSwitchAction,
  onNavigateToAction,
  sampleColumnValues,
  onOpenDiagnose,
  onBackfill,
  onMaterializePdfTables,
  onExportPdfTables,
  onOpenOcrCompare,
  resolveParams,
  estimateAction,
  onExecute,
  onClose,
}: GeneratedActionFormProps & { pluginUI?: GeneratedActionCustomization }) {
  const columns = sheet?.columns ?? EMPTY_COLUMNS;
  const customization = generatedActionCustomizationFor(catalogEntry.kind, pluginUI);
  const ParamsBody = customization?.body;
  const [editorState, setEditorState] = useState<{
    owner: typeof ParamsBody; action: string; message: string | null;
  } | null>(null);
  const setEditorProblem = useCallback((message: string | null) => {
    setEditorState({ owner: ParamsBody, action: catalogEntry.kind, message });
  }, [ParamsBody, catalogEntry.kind]);
  const editorProblem = editorState?.owner === ParamsBody && editorState?.action === catalogEntry.kind
    ? editorState.message : null;
  const renderedParams = useMemo(() => {
    const params = actionTemplate.params ?? [];
    const order = customization?.fieldOrder;
    if (!order?.length) return params;
    const rank = new Map(order.map((name, index) => [name, index]));
    return params.map((param, index) => ({ param, index })).sort((left, right) => (
      (rank.get(left.param.name) ?? order.length + left.index)
      - (rank.get(right.param.name) ?? order.length + right.index)
    )).map(({ param }) => param);
  }, [actionTemplate.params, customization?.fieldOrder]);
  const dynamicOutputs = catalogEntry.ui_hints.dynamic_outputs === true;
  const staticCreatesSheet = catalogEntry.ui_hints.typed_action?.creates_sheet === true;
  const [resolvedCreatesSheet, setResolvedCreatesSheet] = useState<boolean>();
  const createsSheet = resolvedCreatesSheet ?? staticCreatesSheet;
  // A saved table producer may read an exact subset. Creating a destination
  // does not authorize widening that admitted source to the whole project.
  const savedTableScope = createsSheet && initialDraft?.scope.kind === 'sheet_rows'
    ? initialDraft.scope : undefined;
  const [sheetName, setSheetName] = useState(initialDraft?.sheet_name
    ?? customization?.defaultSheetName ?? (sheet ? `${sheet.name} rows` : actionTemplate.name));
  const [renameText, setRenameText] = useState(JSON.stringify(initialDraft?.output_names ?? {}, null, 2));
  const [renameProblem, setRenameProblem] = useState<string | null>(null);
  const sheetId = sheet ? Number(sheet.id) : null;
  const projectScoped = isProjectScopedAction(catalogEntry, createsSheet);
  const allowedScopes = catalogEntry.row_scope_policy == null
    ? ['all_rows', 'exact_membership'] as const
    : catalogEntry.row_scope_policy.kind === 'sheet_rows'
      ? catalogEntry.row_scope_policy.selectors
      : [];
  const defaultScope = (hasExactRowScopeInitializer || selectedRowIds.length > 0)
    && allowedScopes.includes('exact_membership') ? 'selected' : 'all';
  const [runScope, setRunScope] = useState<'all' | 'selected'>(defaultScope);
  const effectiveRunScope = runScope === 'selected'
    && allowedScopes.includes('exact_membership')
    && (selectedRowIds.length > 0 || hasExactRowScopeInitializer)
    ? 'selected' : allowedScopes.includes('all_rows') ? 'all' : 'selected';
  const scopeFor = useCallback((scope: 'all' | 'selected'): GeneratedActionRequest['scope'] => {
    if (savedTableScope) return savedTableScope;
    if (projectScoped) return { kind: 'project' };
    if (sheetId === null) throw new Error('A row action requires a source sheet.');
    return {
      kind: 'sheet_rows', sheet_id: sheetId,
      ...(scope === 'selected' ? { row_ids: selectedRowIds.map(Number) } : {}),
    };
  }, [savedTableScope, projectScoped, sheetId, selectedRowIds]);
  const requestScope = useMemo(() => scopeFor(effectiveRunScope), [scopeFor, effectiveRunScope]);
  const [draft, setDraft] = useState<CanonicalDraft>(() =>
    initialParams(catalogEntry, actionTemplate, columns, initialSourceColumn, initialDraft, initialPrompt));
  // Display server defaults without adding omitted values to a saved request.
  // Custom bodies and serialization continue to receive the actual draft.
  const displayDraft = useMemo(() => {
    const displayed = { ...draft };
    for (const param of renderedParams) {
      const schema = catalogEntry.input_schema.properties?.[param.name];
      if (displayed[param.name] === undefined && schema && 'default' in schema) {
        displayed[param.name] = schema.default as CanonicalFieldValue;
      }
    }
    return displayed;
  }, [catalogEntry.input_schema.properties, draft, renderedParams]);
  const modelTouched = useRef(new Set(
    Object.keys(initialDraft?.params ?? {}).filter(
      (name) => catalogEntry.ui_hints.semantic_controls[name] === 'model',
    ),
  ));
  const [paramsEdited, setParamsEdited] = useState(false);
  const [richSourceEditors, setRichSourceEditors] = useState<
  Record<string, RichSourceEditorState>>(() => (
    Object.fromEntries(Object.entries(catalogEntry.ui_hints.semantic_controls)
      .filter(([, control]) => control === 'rich_source' || control === 'column_or_template')
      .map(([name, control]) => {
        const value = draft[name];
        const requirement = catalogEntry.ui_hints.source_requirements
          ?.find((candidate) => candidate.param === name) ?? null;
        const fallback = compatibleSourceColumns(orderedSourceColumns(columns), requirement)
          .slice(0, Math.max(1, requirement?.min ?? 1))
          .map((column) => column.name);
        return [name, {
          mode: control === 'column_or_template' && typeof value === 'string'
            ? 'column' : Array.isArray(value) ? 'columns' : 'template',
          columns: Array.isArray(value)
            ? value.filter((item): item is string => typeof item === 'string')
            : typeof value === 'string' && control === 'column_or_template' ? [value] : fallback,
          template: templateText(value),
        }];
      }))
  ));
  const [outputNames, setOutputNames] = useState<Record<string, string>>(() =>
    initialOutputNames(catalogEntry, columns, draft, initialDraft));
  const [outputPrefix, setOutputPrefix] = useState(() => {
    const prefix = customization?.outputPrefix;
    const initial = prefix?.keys?.[0]
      ? outputNames[prefix.keys[0]] ?? prefix.initialValue(initialDraft?.output_names)
      : prefix?.initialValue(initialDraft?.output_names) ?? '';
    return prefix?.keys?.length && !initialDraft
      ? dedupeOutputPrefix(prefix, columns, catalogEntry.ui_hints.logical_outputs)
      : initial;
  });
  const editedOutputNames = useRef(new Set(Object.keys(initialDraft?.output_names ?? {})));
  const [editedOutputNameKeys, setEditedOutputNameKeys] = useState(() => new Set(
    Object.keys(initialDraft?.output_names ?? {}),
  ));
  const markOutputNamesEdited = (keys: readonly string[]) => {
    keys.forEach((key) => editedOutputNames.current.add(key));
    setEditedOutputNameKeys((current) => {
      const next = new Set(current);
      keys.forEach((key) => next.add(key));
      return next;
    });
  };

  useEffect(() => {
    if (initialDraft || dynamicOutputs || !customization?.defaultOutputName) return;
    setOutputNames((current) => {
      const next = Object.fromEntries(catalogEntry.ui_hints.logical_outputs.map(({ key }) => [
        key,
        editedOutputNames.current.has(key)
          ? current[key] ?? ''
          : dedupeDefaultColumnName(
              customization.defaultOutputName?.(key, draft) ?? key,
              columns,
            ),
      ]));
      return Object.keys(next).every((key) => next[key] === current[key])
        && Object.keys(next).length === Object.keys(current).length
        ? current
        : next;
    });
  }, [catalogEntry.ui_hints.logical_outputs, customization, draft, dynamicOutputs, initialDraft, columns]);
  const [resolved, setResolved] = useState<{
    outputs: ActionParamResolution['logical_outputs'];
    diagnostics: ActionParamResolution['diagnostics'];
    problem: string | null;
  }>({
    outputs: dynamicOutputs ? [] : catalogEntry.ui_hints.logical_outputs,
    diagnostics: {},
    problem: dynamicOutputs ? 'Resolving outputs…' : 'Validating fields…',
  });
  const { outputs, diagnostics, problem: resolutionProblem } = resolved;
  const resolving = resolutionProblem === 'Resolving outputs…'
    || resolutionProblem === 'Validating fields…';
  useEffect(() => {
    const modelParams = Object.entries(catalogEntry.ui_hints.semantic_controls)
      .flatMap(([name, control]) => control === 'model' ? [name] : []);
    if (!modelParams.length) return undefined;
    let current = true;
    listProviders()
      .then((catalog) => {
        if (!current) return;
        const preferred = defaultActionModel(catalog);
        if (!preferred) return;
        setDraft((before) => {
          let after = before;
          for (const name of modelParams) {
            const existing = before[name];
            const pairedChoice = customization?.engineModelChoice;
            // A fixed-engine leaf deliberately clears its paired model. The
            // provider-catalog default must not race that atomic selection
            // and silently turn a fixed run back into an LLM run.
            if (pairedChoice?.modelParam === name
              && before[pairedChoice.engineParam] !== pairedChoice.providerEngineId) continue;
            if (modelTouched.current.has(name)
              || (typeof existing === 'string' && existing.trim())) continue;
            after = setCanonicalDraftField(actionTemplate, after, name, preferred);
          }
          return after;
        });
      })
      .catch(() => {
        // ModelPicker owns the provider-configuration state. A failed lookup
        // leaves the required field empty rather than implying a usable model.
      });
    return () => {
      current = false;
    };
  }, [actionTemplate, catalogEntry.ui_hints.semantic_controls, customization?.engineModelChoice]);

  useEffect(() => {
    let current = true;
    const timer = window.setTimeout(() => {
      resolveParams({
        action_id: catalogEntry.kind,
        scope: requestScope,
        params: serializeCanonicalDraft(draft),
      })
        .then((resolution) => {
          if (!current) return;
          const diagnostic = Object.values(resolution.diagnostics).find((item) => !item.ok);
          const nextOutputs = dynamicOutputs
            ? resolution.logical_outputs : catalogEntry.ui_hints.logical_outputs;
          const nextCreatesSheet = resolution.creates_sheet ?? staticCreatesSheet;
          let problem = diagnostic?.message ?? (diagnostic ? 'Correct the invalid fields.' : null);
          if (!problem && dynamicOutputs && !nextCreatesSheet && initialDraft && !paramsEdited) {
            const logicalKeys = nextOutputs.map(({ key }) => key);
            const savedKeys = Object.keys(initialDraft.output_names);
            if (savedKeys.some((key) => !logicalKeys.includes(key))) {
              problem = 'Saved action outputs no longer match its parameters.';
            }
            if (initialDraft.sheet_name !== undefined) {
              problem = 'The saved sheet destination does not match this action’s resolved output. Change its parameters or recreate the action.';
            }
          }
          setResolved({ outputs: problem && dynamicOutputs ? [] : nextOutputs,
            diagnostics: resolution.diagnostics, problem });
          if (!problem) setResolvedCreatesSheet(resolution.creates_sheet);
          if (!problem && dynamicOutputs) {
            setOutputNames((names) => {
              if (nextCreatesSheet) {
                // A successful current schema is authoritative after editing
                // Params. Keep surviving renames; removed fields have no
                // editable control and must not leave the action blocked.
                // An undiscovered table schema still accepts its rename map.
                if (!paramsEdited || !nextOutputs.length) return names;
                const keys = new Set(nextOutputs.map(({ key }) => key));
                return Object.fromEntries(Object.entries(names).filter(([key]) => keys.has(key)));
              }
              const sameKeys = Object.keys(names).length === nextOutputs.length
                && nextOutputs.every(({ key }) => Object.hasOwn(names, key));
              return Object.fromEntries(nextOutputs.map(({ key }) => [
                key,
                sameKeys ? names[key]
                  : !paramsEdited && initialDraft ? initialDraft.output_names[key] ?? key
                  : customization?.outputPrefix && (
                    customization.outputPrefix.keys === undefined
                    || customization.outputPrefix.keys.includes(key)
                  )
                    ? customization.outputPrefix.outputName(key, outputPrefix.trim(), nextOutputs)
                    : names[key] ?? dedupeDefaultColumnName(
                        customization?.defaultOutputName?.(key, draft) ?? key, columns,
                      ),
              ]));
            });
          }
        })
        .catch((error: unknown) => {
          if (!current) return;
          setResolved(dynamicOutputs
            ? { outputs: [], diagnostics: {}, problem: error instanceof Error
                ? error.message : 'Could not resolve action outputs.' }
            : { outputs: catalogEntry.ui_hints.logical_outputs, diagnostics: {},
                problem: error instanceof Error
                  ? error.message : 'Could not validate action parameters.' });
        });
    }, 150);
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [actionTemplate, catalogEntry.kind, catalogEntry.ui_hints.logical_outputs, draft,
    staticCreatesSheet, requestScope, customization, dynamicOutputs, initialDraft, outputPrefix, paramsEdited, resolveParams,
    columns, sheetId]);

  const updateField = (name: string, value: CanonicalFieldValue) => {
    if (catalogEntry.ui_hints.semantic_controls[name] === 'model') {
      modelTouched.current.add(name);
    }
    setParamsEdited(true);
    setResolved((current) => ({
      ...current,
      diagnostics: {},
      problem: dynamicOutputs ? 'Resolving outputs…' : 'Validating fields…',
    }));
    const schema = catalogEntry.input_schema.properties?.[name];
    if (value === '' && schema?.default === null) {
      setDraft((current) => setCanonicalDraftField(actionTemplate, current, name, null));
      return;
    }
    const numeric = typeof value === 'string' && value !== '' ? Number(value) : value;
    const normalized = schema?.type === 'integer' && typeof numeric === 'number'
      && Number.isInteger(numeric) ? numeric
      : schema?.type === 'number' && typeof numeric === 'number' && Number.isFinite(numeric)
        ? numeric : value;
    setDraft((current) => setCanonicalDraftField(actionTemplate, current, name, normalized));
  };
  const updateBodyParams = useCallback((params: CanonicalDraft) => {
    setParamsEdited(true);
    setResolved((current) => ({
      ...current,
      diagnostics: {},
      problem: dynamicOutputs ? 'Resolving outputs…' : 'Validating fields…',
    }));
    setDraft(params);
  }, [dynamicOutputs]);
  const renderField = (field: ActionParamFieldPresentationProps): ReactNode | undefined => {
    const CustomField = customization?.fields?.[field.name];
    if (CustomField) {
      return <CustomField name={field.name} label={field.label} id={field.id}
        testid={field.testid} value={draft[field.name]} sheet={sheet}
        error={diagnostics[field.name]} onChange={(value) => updateField(field.name, value)} />;
    }
    const semanticControl = catalogEntry.ui_hints.semantic_controls[field.name];
    if (semanticControl === 'engine') {
      const engines = actionTemplate.engines ?? [];
      const raw = displayDraft[field.name];
      const defaultEngine = catalogEntry.input_schema.properties?.[field.name]?.default;
      const selected = typeof raw === 'string' ? raw : '';
      const ordinaryChoices = [];
      if (defaultEngine === 'auto' && !engines.some((engine) => engine.id === 'auto')) {
        ordinaryChoices.push({ id: 'auto', label: 'Auto',
          available: engines.some((engine) => engine.available !== false),
          unavailableReason: 'No execution engines are available.' });
      }
      if (selected && !engines.some((engine) => engine.id === selected)
        && !ordinaryChoices.some((choice) => choice.id === selected)) {
        ordinaryChoices.push({ id: selected, label: `${selected} (unavailable)`, available: false,
          unavailableReason: 'This engine is unavailable for this action.' });
      }
      return <div className={pickerStyles.choice} data-testid={field.testid}>
        <span className={`form-label ${pickerStyles.label}`} id={field.id}>{field.label}</span>
        <div className={pickerStyles.picker}>
          <EnginePicker engines={engines} ordinaryChoices={ordinaryChoices} value={selected}
            onChange={(value) => updateField(field.name, value)} ariaLabelledBy={field.id} />
        </div>
        {engines.some((engine) => engine.available === false) && (
          <details className="action-advanced" data-testid="engine-availability-disclosure">
            <summary>Engine availability</summary>
            {engines.filter((engine) => engine.available === false).map((engine) => (
              <p className="form-hint" key={engine.id}
                data-testid={`engine-availability-line-${engine.id}`}>
                {engine.label}: unavailable{engine.error && ` — ${engine.error}`}
              </p>
            ))}
            {onOpenDiagnose && <button type="button" className="btn btn-secondary"
              data-testid="engine-availability-open-diagnose" onClick={onOpenDiagnose}>
              Open Diagnose
            </button>}
          </details>
        )}
      </div>;
    }
    if (semanticControl === 'rich_source' || semanticControl === 'column_or_template') {
      const singleColumn = semanticControl === 'column_or_template';
      const requirement = catalogEntry.ui_hints.source_requirements
        ?.find((candidate) => candidate.param === field.name) ?? null;
      const raw = draft[field.name];
      const editor = richSourceEditors[field.name] ?? {
        mode: singleColumn && typeof raw === 'string' ? 'column'
          : Array.isArray(raw) ? 'columns' : 'template',
        columns: Array.isArray(raw)
          ? raw.filter((value): value is string => typeof value === 'string')
          : singleColumn && typeof raw === 'string' ? [raw] : [],
        template: templateText(raw),
      };
      const sourceColumns = compatibleSourceColumns(orderedSourceColumns(columns),
        editor.mode === 'template' && requirement?.template_accepted_column_types
          ? { ...requirement, accepted_column_types: requirement.template_accepted_column_types }
          : requirement);
      const writeSource = (next: RichSourceEditorState) => {
        setRichSourceEditors((current) => ({ ...current, [field.name]: next }));
        updateField(
          field.name,
          next.mode === 'template' ? { text: next.template }
            : singleColumn ? next.columns[0] ?? '' : next.columns,
        );
      };
      const sourceHint = catalogEntry.input_schema.properties?.[field.name]?.description;
      return (
        <>
          <SourceInputControl
            mode={editor.mode}
            columns={sourceColumns}
            selectedColumn={singleColumn ? editor.columns[0] ?? '' : ''}
            template={editor.template}
            label={field.label}
            testIdPrefix="text"
            emptyMessage={requirement?.message ?? 'No compatible source columns.'}
            ariaLabel={`${field.label} columns`}
            multiColumn={singleColumn ? undefined : {
              selectedColumns: editor.columns,
              allModeLabel: 'All compatible columns',
              onSelectedColumnsChange: (names) => writeSource({
                ...editor,
                mode: 'columns',
                columns: names,
              }),
            }}
            onModeChange={(nextMode) => {
              if (nextMode === 'all') {
                writeSource({
                  ...editor,
                  mode: 'all',
                  columns: compatibleSourceColumns(orderedSourceColumns(columns), requirement)
                    .slice(0, requirement?.max)
                    .map((column) => column.name),
                });
              } else if (nextMode === 'template') {
                writeSource({ ...editor, mode: 'template' });
              } else {
                writeSource({ ...editor, mode: singleColumn ? 'column' : 'columns' });
              }
            }}
            onSelectedColumnChange={(name) => writeSource({ ...editor, mode: 'column', columns: [name] })}
            onTemplateChange={(value) => writeSource({
              ...editor,
              mode: 'template',
              template: value,
            })}
          />
          {typeof sourceHint === 'string' && <p className="form-hint">{sourceHint}</p>}
        </>
      );
    }
    if (semanticControl === 'model') {
      return (
        <div className={pickerStyles.choice} data-testid={field.testid}>
          <span className={`form-label ${pickerStyles.label}`} id={field.id}>{field.label}</span>
          <div className={pickerStyles.picker}>
          <ModelPicker value={field.value}
            onChange={(value) => updateField(field.name, value)}
            ariaLabelledBy={field.id} />
          </div>
        </div>
      );
    }
    if (semanticControl === 'template') {
      return (
        <>
          <label className="form-label" htmlFor={field.id}>{field.label}</label>
          <TemplateComposer value={templateText(draft[field.name])} columns={columns}
            textareaTestId={field.testid} insertTestId={`${field.testid}-column-insert`}
            ariaLabel={field.label} onChange={(text) => updateField(field.name, { text })} />
        </>
      );
    }
    const schemaType = catalogEntry.input_schema.properties?.[field.name]?.type;
    const param = actionTemplate.params?.find((candidate) => candidate.name === field.name);
    if (!semanticControl && (schemaType === 'number' || schemaType === 'integer')) {
      return <NumericSchemaField field={field} value={draft[field.name]}
        onChange={(value) => updateField(field.name, value)} />;
    }
    if (!semanticControl && param?.input !== 'columns'
      && (schemaType === 'object' || schemaType === 'array')) {
      return <JsonSchemaField field={field} value={draft[field.name]} schemaType={schemaType}
        onChange={(value) => updateField(field.name, value)} />;
    }
    return undefined;
  };

  const engineParam = Object.entries(catalogEntry.ui_hints.semantic_controls)
    .find(([, control]) => control === 'engine')?.[0];
  const selectedEngine = engineParam && typeof displayDraft[engineParam] === 'string'
    ? displayDraft[engineParam] : undefined;
  const selectedEngineInfo = actionTemplate.engines?.find((engine) => engine.id === selectedEngine);
  const automaticEngineSupported = engineParam
    && (catalogEntry.input_schema.properties?.[engineParam]?.default === 'auto'
      || actionTemplate.engines?.some((engine) => engine.id === 'auto'));
  const automaticEngineAvailable = actionTemplate.engines?.some(
    (engine) => engine.id !== 'auto' && engine.available !== false,
  );
  const engineProblem = selectedEngineInfo?.available === false
    ? selectedEngineInfo.error || `${selectedEngineInfo.label} is unavailable.`
    : selectedEngine === 'auto' && automaticEngineSupported
      ? automaticEngineAvailable ? null : 'No execution engines are available.'
      : selectedEngine && !selectedEngineInfo ? `${selectedEngine} is unavailable.` : null;
  const required = new Set(catalogEntry.input_schema.required ?? []);
  const validParams = (actionTemplate.params ?? []).every((param) => {
    const value = draft[param.name];
    if (value === undefined) return !required.has(param.name);
    const semantic = catalogEntry.ui_hints.semantic_controls[param.name];
    if (semantic === 'template'
      || (semantic === 'rich_source' && !Array.isArray(value))
      || (semantic === 'column_or_template' && typeof value !== 'string')) {
      return templateText(value).trim().length > 0;
    }
    const schemaType = catalogEntry.input_schema.properties?.[param.name]?.type;
    if ((schemaType === 'object' || schemaType === 'array') && typeof value === 'string') return false;
    if (schemaType === 'integer' && value !== '' && !Number.isInteger(value)) return false;
    if (schemaType === 'number' && value !== ''
      && (typeof value !== 'number' || !Number.isFinite(value))) return false;
    if (!required.has(param.name)) return true;
    if (typeof value === 'string') {
      if (!value.trim()) return false;
      if (semantic === 'column' || semantic === 'column_or_template') {
        const requirement = catalogEntry.ui_hints.source_requirements?.find(
          (candidate) => candidate.param === param.name,
        ) ?? null;
        return compatibleSourceColumns(orderedSourceColumns(columns), requirement)
          .some((column) => column.name === value);
      }
    }
    return !Array.isArray(value) || value.length > 0;
  });
  const validIdentity = (!createsSheet || sheetName.trim().length > 0)
    && (requestScope.kind === 'project' || (Number.isSafeInteger(requestScope.sheet_id) && requestScope.sheet_id > 0
      && selectedRowIds.every((rowId) => Number.isSafeInteger(Number(rowId)) && Number(rowId) > 0)));
  // Dynamic names can only be compared with a successfully resolved schema,
  // not the empty placeholder used while resolving or reporting a refusal.
  const outputNameProblem = renameProblem ?? (dynamicOutputs && resolutionProblem
    ? null : validateOutputNames(catalogEntry, outputs, outputNames, createsSheet));
  const staleOutputNames = createsSheet && dynamicOutputs && !resolutionProblem && outputs.length
    ? Object.keys(outputNames).filter((key) => !outputs.some((output) => output.key === key)) : [];
  const missingCredentials = actionTemplate.missingCredentials ?? [];
  const canRun = catalogAccepted && missingCredentials.length === 0 && !resolutionProblem
    && validIdentity && validParams && !outputNameProblem && !engineProblem && !editorProblem;
  const costSource = selectedEngine && selectedEngine !== 'auto'
    ? actionTemplate.costSourceOptions?.[selectedEngine] : actionTemplate.costSource;
  const freePublicApi = costSource === 'free_public_api';
  // A policy with no execution charge names that fact directly. Every other
  // action gets the same debounced, request-identity-fenced estimate whether
  // or not this particular amount will need a confirmation click.
  const priceable = !freePublicApi && catalogEntry.cost_policy?.kind !== 'none';
  const estimateDraft = useMemo(() => ({
    action_id: catalogEntry.kind,
    scope: requestScope,
    params: serializeCanonicalDraft(draft),
    ...(createsSheet ? { sheet_name: sheetName.trim() } : {}),
    output_names: Object.fromEntries(Object.entries(outputNames)
      .map(([key, value]) => [key, value.trim()])),
  }), [catalogEntry.kind, draft, createsSheet, requestScope, sheetName, outputNames]);
  const estimateIdentity = JSON.stringify(estimateDraft);
  const [estimateState, setEstimateState] = useState<{
    identity: string;
    estimate: RunEstimate | null;
  }>({ identity: '', estimate: null });

  useEffect(() => {
    if (!canRun || !estimateAction || !priceable) return undefined;
    let current = true;
    const timer = window.setTimeout(() => {
      estimateAction({
        ...estimateDraft,
        idempotency_key: freshActionRequestKey(catalogEntry.kind),
      })
        .then((estimate) => {
          if (current) setEstimateState({ identity: estimateIdentity, estimate });
        })
        .catch(() => {
          if (current) setEstimateState({ identity: estimateIdentity, estimate: null });
        });
    }, 350);
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [canRun, catalogEntry.kind, priceable,
    estimateAction, estimateDraft, estimateIdentity]);

  const currentEstimate = canRun && estimateState.identity === estimateIdentity
    ? estimateState.estimate : null;
  const currentEstimatePending = priceable && canRun
    && estimateState.identity !== estimateIdentity;
  const quotedEstimate = quotedUsd(currentEstimate);
  const venueLabel = currentEstimate?.venue_label?.trim() || null;
  const billingLabel = currentEstimate?.billing_label?.trim() || null;
  const estimateWarning = currentEstimate?.warning?.trim() || null;
  const estimateRowCount = currentEstimate?.rows
    ?? (sheet ? effectiveRunScope === 'selected' ? selectedRowIds.length : sheet.rowCount : null);
  const outputColumnCount = dynamicOutputs && resolving ? 0 : outputs.length;

  const primaryOutputName = outputs[0] ? outputNames[outputs[0].key]?.trim() : '';
  const backfillTargetColumn = primaryOutputName
    ? columns.find((column) => (
        column.name === primaryOutputName && column.ai !== undefined
      )) ?? null
    : null;
  const backfillVersions = backfillTargetColumn?.ai?.versions ?? [];
  const latestBackfillVersion = backfillVersions.length > 0
    ? backfillVersions.reduce((best, version) => (
        version.version > best.version ? version : best
      ))
    : null;
  const backfillHasGaps = latestBackfillVersion?.status === 'partial';

  const submit = (scope: 'all' | 'selected', intent: 'preview' | 'run') => {
    if (!canRun || (!projectScoped && scope === 'selected'
      && selectedRowIds.length === 0 && !hasExactRowScopeInitializer)) return;
    const request = {
      action_id: catalogEntry.kind,
      scope: scopeFor(scope),
      params: serializeCanonicalDraft(draft),
      ...(createsSheet ? { sheet_name: sheetName.trim() } : {}),
      output_names: Object.fromEntries(Object.entries(outputNames)
        .map(([key, value]) => [key, value.trim()])),
      idempotency_key: freshActionRequestKey(catalogEntry.kind),
    } satisfies GeneratedActionRequest;
    if (intent === 'preview') {
      onExecute(request, intent);
      return;
    }
    onExecute(request, intent);
  };

  const ocrCompareTarget: OcrCompareTarget = {
    engine: typeof draft.engine === 'string' ? draft.engine : undefined,
    language: typeof draft.language === 'string' ? draft.language : undefined,
    dpi: typeof draft.dpi === 'number' ? draft.dpi : undefined,
    searchable_pdf: typeof draft.searchable_pdf === 'boolean' ? draft.searchable_pdf : undefined,
  };

  const costLine = <>
    {!freePublicApi && catalogEntry.cost_policy?.kind === 'none' && (
      <div className="cost-line cost-local" data-testid="cost-estimate">
        No execution charge: <strong>$0.00</strong>
      </div>
    )}
    {(freePublicApi || priceable) && (
      <div className={`cost-line ${freePublicApi || (!currentEstimatePending && quotedEstimate === 0)
        ? 'cost-local' : 'cost-paid'}`} data-testid="cost-estimate">
        <span className="cost-dot" aria-hidden />
        {freePublicApi ? <>
          <strong>No provider/API charge</strong> · {customization?.freePublicApiLabel ?? 'Free public API request'}
        </> : currentEstimatePending
          ? 'Estimating cost…'
          : <>Estimated cost: <strong>{quotedEstimate === null
            ? 'UNKNOWN' : formatUsd(quotedEstimate)}</strong>{currentEstimate?.audio_seconds != null
              ? ` · ${formatDuration(currentEstimate.audio_seconds * 1000)} of audio`
              : estimateRowCount !== null
                ? ` · ${estimateRowCount.toLocaleString()} ${estimateRowCount === 1 ? 'row' : 'rows'}`
                : null}
            {outputColumnCount > 0
              ? ` · ${outputColumnCount} output ${outputColumnCount === 1 ? 'column' : 'columns'}`
              : null}
            {venueLabel ? <> · <span data-testid="cost-venue-label">{venueLabel}</span></> : null}
            {billingLabel ? <> · <span data-testid="cost-billing-label">{billingLabel}</span></> : null}
            {estimateWarning ? <> · <span data-testid="cost-estimate-warning">{estimateWarning}</span></> : null}
          </>}
      </div>
    )}
  </>;

  return (
    <form className="action-form generated-action-form" data-testid="generated-action-form"
      onSubmit={(event) => {
        event.preventDefault();
        submit(effectiveRunScope, 'run');
      }}>
      <ActionFormHeader actionTemplate={actionTemplate} title={title} sheetName={sheet?.name}
        switchActions={switchActions} onSwitchAction={onSwitchAction}
        variant="drawer" onClose={onClose} />
      <ActionCredentialGate actionTemplate={actionTemplate} />
      {sheet && onMaterializePdfTables && <PdfTablesReview actionKind={catalogEntry.kind}
        sheet={sheet} outputName={outputNames.pdf_tables ?? ''}
        onMaterialize={onMaterializePdfTables} onExport={onExportPdfTables} />}
      {engineProblem && <p className="form-hint" role="alert">{engineProblem}</p>}

      {createsSheet && <div className="action-source-block">
        <label className="form-label" htmlFor="generated-sheet-name">New sheet name</label>
        <input id="generated-sheet-name" className="form-input" data-testid="field-sheet_name"
          value={sheetName} disabled={running} onChange={(event) => setSheetName(event.target.value)} />
      </div>}

      {ParamsBody ? (
        <fieldset disabled={running} className="resolve-drawer-body-fields">
          <GeneratedFieldContext.Provider value={{ actionTemplate, draft: displayDraft, columns,
            diagnostics, renderField, onFieldChange: updateField }}>
            <ParamsBody sheet={sheet} params={draft} setParams={updateBodyParams}
              setEditorProblem={setEditorProblem}
              engine={selectedEngineInfo}
              engines={actionTemplate.engines}
              engineModelChoice={customization?.engineModelChoice}
              request={{ scope: requestScope, sheet_name: estimateDraft.sheet_name,
                output_names: estimateDraft.output_names }}
              errors={diagnostics} Field={GeneratedField} onNavigateToAction={onNavigateToAction}
              sampleColumnValues={sampleColumnValues} />
          </GeneratedFieldContext.Provider>
        </fieldset>
      ) : (
        <GenericSchemaRenderer
          template={{ ...actionTemplate, params: renderedParams.map((param) => (
            { ...param, visibleWhen: undefined }
          )) }}
          draft={displayDraft}
          columns={columns}
          diagnostics={diagnostics}
          renderField={renderField}
          onFieldChange={updateField}
        />
      )}

      {editorProblem && <p className="form-error" role="alert" data-testid="generated-editor-problem">{editorProblem}</p>}

      {catalogEntry.kind === 'media.ocr' && (
        <div className="form-actions">
          <button type="button" className="btn" data-testid="ocr-compare-open"
            disabled={running || !onOpenOcrCompare}
            onClick={() => onOpenOcrCompare?.(ocrCompareTarget)}>
            Compare engines
          </button>
        </div>
      )}

      {customization?.outputPrefix && <div className="param-row">
        <label className="form-label" htmlFor="generated-output-prefix">
          {customization.outputPrefix.label} column {outputs.length === 1 ? 'name' : 'prefix'}
        </label>
        <input id="generated-output-prefix" className="form-input"
          data-testid="field-output-prefix" value={outputPrefix} disabled={running}
          onChange={(event) => {
            const prefix = event.target.value;
            const keys = customization.outputPrefix!.keys ?? outputs.map(({ key }) => key);
            setOutputPrefix(prefix);
            markOutputNamesEdited(keys);
            setOutputNames((current) => Object.fromEntries(outputs.map(({ key }) => [key,
              keys.includes(key)
                ? customization.outputPrefix!.outputName(key, prefix.trim(), outputs)
                : current[key] ?? dedupeDefaultColumnName(
                    customization.defaultOutputName?.(key, draft) ?? key, columns,
                  ),
            ])));
          }} />
        {customization.outputPrefix.hint !== null && <p className="form-hint">
          {customization.outputPrefix.hint === undefined ? <>Names {outputs.length} output {outputs.length === 1 ? 'column' : 'columns'}.
            Individual names can be adjusted below.</> : customization.outputPrefix.hint}
        </p>}
      </div>}

      {catalogEntry.kind === 'derive.join' && outputs.length > 0 && <JoinOutputNames
        outputs={outputs} names={outputNames} disabled={running || resolving}
        onChange={(names) => {
          markOutputNamesEdited(Object.keys(names));
          setOutputNames(names);
        }} />}

      {!customization?.outputNamesReadOnly && outputs.filter(({ key }) => (
        !customization?.hiddenOutputNameKeys?.includes(key)
      )).map(({
        key, column_type: columnType, existing_column_policy: existingColumnPolicy,
      }) => {
        const defaultNameCollision = !initialDraft && !editedOutputNameKeys.has(key)
          ? automaticOutputNameCollision(key, draft, columns, customization) : null;
        return <Fragment key={key}>
          <OutputNameCombobox
            columns={createsSheet ? [] : compatibleOutputColumns(
              columns,
              columnType,
              existingColumnPolicy,
            )}
            label={customization?.outputLabel?.(key) ?? (outputs.length === 1 ? 'Save to'
              : `Save ${key.replace(/_/g, ' ').replace(/^./, (letter) => letter.toUpperCase())} to`)}
            inputTestId={`field-output-${key}`} value={outputNames[key] ?? (createsSheet ? key : '')}
            allowExistingTargets={!createsSheet}
            onChange={(value) => {
              markOutputNamesEdited([key]);
              setOutputNames((current) => ({ ...current, [key]: value }));
            }} />
          {defaultNameCollision && outputNames[key] === defaultNameCollision.renamed && (
            <p className="form-hint" data-testid={`default-output-name-collision-${key}`}>
              A "{defaultNameCollision.original}" column already exists — saving to "
              {defaultNameCollision.renamed}".
            </p>
          )}
        </Fragment>;
      })}

      {createsSheet && !customization?.outputNamesReadOnly && outputs.length === 0 && <details className="action-advanced">
        <summary>Rename output columns</summary>
        <p className="form-hint">Columns are discovered from the list. Optionally map their names to new names.</p>
        <textarea className="form-input" aria-label="Output column renames" value={renameText}
          disabled={running} onChange={(event) => {
            setRenameText(event.target.value);
            try {
              const value: unknown = JSON.parse(event.target.value);
              if (!value || typeof value !== 'object' || Array.isArray(value)
                || Object.values(value).some((name) => typeof name !== 'string')) throw new Error();
              setOutputNames(value as Record<string, string>);
              setRenameProblem(null);
            } catch {
              setRenameProblem('Enter a JSON object mapping column names to new names.');
            }
          }} />
      </details>}

      {outputNameProblem && <p className="form-error" role="alert">{outputNameProblem}</p>}
      {staleOutputNames.length > 0 && <div className="form-hint" data-testid="stale-output-names">
        Saved names refer to unavailable outputs: {staleOutputNames.join(', ')}.
        <button type="button" className="btn btn-ghost" disabled={running}
          onClick={() => setOutputNames((names) => Object.fromEntries(
            Object.entries(names).filter(([key]) => !staleOutputNames.includes(key)),
          ))}>Remove unavailable output names</button>
      </div>}

      {resolutionProblem && !resolving
        && (ParamsBody || !Object.entries(diagnostics).some(([name, diagnostic]) => (
          name !== '__all__' && !diagnostic.ok
        ))) && (
        <p className="form-error" role="alert">{resolutionProblem}</p>
      )}

      {projectScoped ? <div className="form-actions action-run-actions">
        {costLine}
        <div className="run-actions-row">
          <button type="button" className="btn" data-testid="generated-action-preview"
            disabled={!canRun || running} onClick={() => submit('all', 'preview')}>
            <Eye size={13} /> Preview
          </button>
          <button type="submit" className="btn btn-primary" data-testid="run-button"
            disabled={!canRun || running}>{running ? runningLabel : customization?.primaryLabel ?? (createsSheet ? 'Create sheet' : 'Run')}</button>
        </div>
      </div> : <RunScopeFooter primaryLabel={customization?.primaryLabel ?? 'Run'} rowCount={sheet?.rowCount ?? 0}
        selectedCount={selectedRowIds.length} canRun={canRun}
        scope={runScope} onScopeChange={setRunScope}
        runOnScopeSelect={catalogEntry.cost_policy?.requires_confirmation !== true
          || (!currentEstimatePending && quotedEstimate === 0)}
        allowedScopes={allowedScopes}
        allowEmptySelection={hasExactRowScopeInitializer}
        primaryButtonType="submit"
        backfillTargetName={backfillTargetColumn?.name ?? null}
        backfillHasGaps={backfillHasGaps}
        onBackfill={onBackfill}
        running={running} runningLabel={runningLabel}
        primaryTestId={customization?.primaryTestId}
        disabledReason={!catalogAccepted ? 'The action catalog is refreshing.'
          : missingCredentials.length ? `Configure ${missingCredentials.join(', ')} in Settings before running.`
          : editorProblem ?? engineProblem ?? resolutionProblem ?? outputNameProblem ?? (!validIdentity
          ? 'The selected rows are invalid.'
          : 'Complete the required fields.')}
        testIdPrefix="generated-action"
        onPreview={(scope) => submit(scope, 'preview')}
        onRun={(scope) => submit(scope, 'run')}>{costLine}</RunScopeFooter>}
    </form>
  );
}
