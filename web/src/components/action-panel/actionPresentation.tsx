/* eslint-disable react-refresh/only-export-components -- This is a resolution
 * module whose generic renderer lives alongside the helpers that decide when
 * it applies. */
import { hasNoActionDrawer } from '../../actions/registry';
import type {
  ActionCatalogEntry,
  ActionCatalogPayload,
  ColumnDef,
  ParamValidationResult,
} from '../../api/open';
import { hasGeneratedActionForm, isGeneratedActionCatalogEntry } from '../../api/open';
import type { ActionParam, ActionTemplate } from '../../api/types';
import {
  canonicalActionFieldValue,
  type CanonicalActionDraft as CanonicalDraft,
  type CanonicalActionParamValue as CanonicalFieldValue,
} from '../../actions/canonicalActionDraft';
export {
  buildCanonicalActionDraft as buildCanonicalDraft,
  canonicalActionDraftFromValidatedParams as buildCanonicalDraftFromValidatedParams,
  serializeCanonicalActionDraft as serializeCanonicalDraft,
  setCanonicalActionDraftField as setCanonicalDraftField,
  type CanonicalActionDraft as CanonicalDraft,
  type CanonicalActionParamValue as CanonicalFieldValue,
} from '../../actions/canonicalActionDraft';
import {
  ActionParams,
  type ActionParamFieldPresentationRenderer,
} from './ActionParams';

export type ActionPresentationResolution =
  { kind: 'generated' };

export type ActionPresentationDisposition =
  | ActionPresentationResolution
  | { kind: 'hidden'; reason: 'no_action_drawer_launcher' };

export class ActionPresentationCatalogError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ActionPresentationCatalogError';
  }
}

function own(value: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function hasOnlyGenericMetadata(param: ActionParam): boolean {
  const textControl = param.input === 'text' || param.input === 'textarea';
  const columnControl = param.input === 'column'
    || param.input === 'column-optional'
    || param.input === 'columns';
  return !(
    (param.columnTypes && !columnControl)
    || (param.aiGeneratedOnly && !columnControl)
    || (param.maxLength !== undefined && !textControl)
    || (param.monospace && !textControl)
    || (param.validator && !textControl)
    || param.advancedGroup
    || param.denseGroup
    || param.denseSpan
  );
}

export function isClosedVocabularyControl(param: ActionParam): boolean {
  if (!hasOnlyGenericMetadata(param)) return false;
  if (
    param.input === 'text'
    || param.input === 'textarea'
    || param.input === 'column'
    || param.input === 'column-optional'
    || param.input === 'columns'
  ) {
    return true;
  }
  if (param.input === 'select') {
    if (!param.choices?.length || !param.choiceOptions) return false;
    return param.choices.every((choice) => (
      param.choiceOptions?.some((option) => option.value === choice && option.label.trim())
    ));
  }
  if (param.input !== 'checkbox') return false;
  if (param.layout === undefined && param.group === undefined) return true;
  return param.layout === 'grid' && Boolean(param.group?.trim());
}

export function supportsGenericActionTemplate(template: ActionTemplate): boolean {
  const params = template.params ?? [];
  const names = new Set(params.map((param) => param.name));
  if (names.size !== params.length) return false;
  return params.every((param) => {
    if (!isClosedVocabularyControl(param)) return false;
    return !param.visibleWhen || (
      names.has(param.visibleWhen.param)
      && typeof param.visibleWhen.value === 'string'
    );
  });
}

/** Resolve one already-joined renderable template. Production obtains the
 * template only from deriveActionPresentationCatalog's complete-catalog join;
 * focused component tests use this pure seam to exercise renderer mechanics. */
export function resolveActionPresentation(
  template: ActionTemplate,
): ActionPresentationResolution {
  const canonicalKind = template.actionKind?.trim() ?? '';
  if (!canonicalKind || canonicalKind !== template.actionKind) {
    throw new ActionPresentationCatalogError('renderable action template has no exact canonical kind');
  }
  const authoringContractVersion = template.authoringContractVersion;
  if (authoringContractVersion !== 1) {
    throw new ActionPresentationCatalogError(
      `renderable action template ${canonicalKind} has no valid authoring contract version`,
    );
  }
  if (template.kind !== canonicalKind) {
    throw new ActionPresentationCatalogError(`action ${canonicalKind} has a noncanonical template identity`);
  }
  if (template.generatedAction) return { kind: 'generated' };

  throw new ActionPresentationCatalogError(
    `action ${canonicalKind} has no supported presentation disposition`,
  );
}

/** Derive the complete served presentation partition. Generated and hidden
 * membership are consequences of the
 * catalog/launcher join; no kind declares a bespoke presentation. */
export function deriveActionPresentationCatalog(
  catalog: ActionCatalogPayload,
  resolvedTemplates: readonly ActionTemplate[],
): ReadonlyMap<string, ActionPresentationDisposition> {
  if (!isRecord(catalog) || !Array.isArray(catalog.actions)) {
    throw new ActionPresentationCatalogError('action catalog has no actions array');
  }

  const entriesByKind = new Map<string, ActionCatalogEntry>();
  for (const entry of catalog.actions) {
    if (!isRecord(entry)) {
      throw new ActionPresentationCatalogError('action catalog contains a malformed entry');
    }
    const kind = typeof entry.kind === 'string' ? entry.kind.trim() : '';
    if (!kind || kind !== entry.kind) {
      throw new ActionPresentationCatalogError('action catalog contains an invalid canonical kind');
    }
    if (!entry.ui_hints || typeof entry.ui_hints !== 'object' || Array.isArray(entry.ui_hints)) {
      throw new ActionPresentationCatalogError(`action catalog entry ${kind} has malformed ui_hints`);
    }
    if (entry.authoring_contract_version !== 1) {
      throw new ActionPresentationCatalogError(
        `action catalog entry ${kind} has invalid authoring contract version`,
      );
    }
    const inputSchema = entry.input_schema;
    const schemaProperties = isRecord(inputSchema) ? inputSchema.properties : undefined;
    if (
      !isRecord(inputSchema)
      || !isRecord(schemaProperties)
    ) {
      throw new ActionPresentationCatalogError(`action catalog entry ${kind} has malformed input_schema`);
    }
    if (entriesByKind.has(kind)) {
      throw new ActionPresentationCatalogError(`action catalog declares ${kind} more than once`);
    }
    entriesByKind.set(kind, entry);
  }

  const templatesByKind = new Map<string, ActionTemplate>();
  for (const template of resolvedTemplates) {
    if (!isRecord(template)) {
      throw new ActionPresentationCatalogError('resolved templates contain a malformed entry');
    }
    if (template.actionKind === undefined) continue;
    if (typeof template.actionKind !== 'string' || typeof template.kind !== 'string') {
      throw new ActionPresentationCatalogError('resolved template has malformed action or launcher kind');
    }
    if (template.params !== undefined && !Array.isArray(template.params)) {
      throw new ActionPresentationCatalogError(`resolved template ${template.actionKind} has malformed params`);
    }
    const kind = template.actionKind.trim();
    if (!kind || kind !== template.actionKind) {
      throw new ActionPresentationCatalogError('resolved template has an invalid canonical kind');
    }
    if (!entriesByKind.has(kind)) {
      throw new ActionPresentationCatalogError(
        `resolved template ${kind} has no served catalog entry`,
      );
    }
    if (template.authoringContractVersion !== 1) {
      throw new ActionPresentationCatalogError(
        `resolved template ${kind} has no valid authoring contract version`,
      );
    }
    if (templatesByKind.has(kind)) {
      throw new ActionPresentationCatalogError(`resolved templates declare ${kind} more than once`);
    }
    templatesByKind.set(kind, template);
  }

  const dispositions = new Map<string, ActionPresentationDisposition>();
  for (const entry of catalog.actions) {
    const kind = entry.kind;
    const template = templatesByKind.get(kind);
    if (hasNoActionDrawer(kind)) {
      if (template) {
        throw new ActionPresentationCatalogError(
          `action ${kind} unexpectedly resolved an action-drawer template`,
        );
      }
      dispositions.set(kind, { kind: 'hidden', reason: 'no_action_drawer_launcher' });
      continue;
    }

    if (hasGeneratedActionForm(entry)) {
      if (!isGeneratedActionCatalogEntry(entry)) {
        throw new ActionPresentationCatalogError(
          `generated action ${kind} has an invalid form contract`,
        );
      }
      if (!template || template.kind !== kind || !template.generatedAction) {
        throw new ActionPresentationCatalogError(
          `generated action ${kind} has no canonical launcher template`,
        );
      }
      dispositions.set(kind, resolveActionPresentation(template));
      continue;
    }

    if (template) {
      throw new ActionPresentationCatalogError(`stock action ${kind} resolved a template without a generated form`);
    }
    dispositions.set(kind, { kind: 'hidden', reason: 'no_action_drawer_launcher' });
  }
  return dispositions;
}

function displayedValue(param: ActionParam, draft: CanonicalDraft): string {
  const value = draft[param.name];
  if (value === null || Array.isArray(value)) return '';
  return own(draft, param.name) ? String(value) : param.defaultValue ?? '';
}

export function GenericSchemaRenderer({
  template,
  draft,
  columns,
  diagnostics,
  renderField,
  testIdOverrides,
  onFieldChange,
}: {
  template: ActionTemplate;
  draft: CanonicalDraft;
  columns: ColumnDef[];
  diagnostics?: ParamValidationResult | null;
  renderField?: ActionParamFieldPresentationRenderer;
  testIdOverrides?: Readonly<Record<string, string>>;
  onFieldChange(name: string, value: CanonicalFieldValue): void;
}) {
  if (!supportsGenericActionTemplate(template)) {
    throw new Error(`Unsupported generic action vocabulary for ${template.actionKind ?? template.kind}`);
  }
  const params = template.params ?? [];
  const values = Object.fromEntries((template.params ?? []).map((param) => [
    param.name,
    displayedValue(param, draft),
  ]));
  const columnValues = Object.fromEntries((template.params ?? []).flatMap((param) => {
    const value = draft[param.name];
    return param.input === 'columns'
      && Array.isArray(value)
      && value.every((item) => typeof item === 'string')
      ? [[param.name, value]]
      : [];
  }));
  return (
    <ActionParams
      params={params}
      advancedTestId={`advanced-params-${template.kind}`}
      values={values}
      columnValues={columnValues}
      columns={columns}
      onChange={(name, value) => {
        const param = params.find((candidate) => candidate.name === name);
        if (!param) throw new Error(`Unknown canonical action field: ${name}`);
        onFieldChange(name, canonicalActionFieldValue(param, value));
      }}
      diagnostics={diagnostics}
      renderField={renderField}
      testIdOverrides={testIdOverrides}
    />
  );
}
