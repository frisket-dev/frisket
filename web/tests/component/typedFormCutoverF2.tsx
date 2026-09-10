// @vitest-environment jsdom
//
// Lane-local mount helper (test-cutover-ui-f2) for the typed GeneratedActionForm.
// Mounts a served-catalog entry the way ActionPanel does and hands back the
// typed wire (`action_id/scope/params/output_names/idempotency_key`) that
// `onExecute` receives. Kept beside the three suites it serves; no
// production adapter, no rerouting of legacy ActionForm mounts.

import { render } from '@testing-library/react';
import { vi } from 'vitest';

import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import type {
  EngineOption,
  GeneratedActionCatalogEntry,
  GeneratedActionDraft,
  GeneratedActionRequest,
  RunEstimate,
  SheetMeta,
} from '../../src/api/types';
import { isGeneratedActionCatalogEntry } from '../../src/api/types';
import {
  GeneratedActionForm,
  type GeneratedActionFormProps,
} from '../../src/components/action-panel/GeneratedActionForm';
import { servedActionCatalog } from '../support/servedActionCatalog';

export type TypedResolveParams = GeneratedActionFormProps['resolveParams'];
export type TypedEstimateAction = NonNullable<GeneratedActionFormProps['estimateAction']>;

/** A deep copy of the served catalog entry for `kind`, with every declared
 *  engine marked available (the served fixture carries no availability
 *  facts). `engines` lets a test reshape that roster the way a version-skewed
 *  or partially configured catalog would. */
export function servedTypedEntry(
  kind: string,
  engines?: (served: EngineOption[]) => EngineOption[],
): GeneratedActionCatalogEntry {
  const entry = structuredClone(servedActionCatalog().actions.find((item) => item.kind === kind));
  if (!entry || !isGeneratedActionCatalogEntry(entry)) {
    throw new Error(`${kind} is not a generated catalog entry`);
  }
  const served = (entry.ui_hints.engines ?? []).map((engine) => ({ ...engine, available: true }));
  const roster = engines ? engines(served) : served;
  if (roster.length) entry.ui_hints.engines = roster;
  return entry;
}

export interface MountTypedFormOptions {
  entry: GeneratedActionCatalogEntry;
  sheet: SheetMeta;
  resolveParams: TypedResolveParams;
  estimateAction?: TypedEstimateAction;
  initialDraft?: GeneratedActionDraft;
  initialSourceColumn?: string;
  selectedRowIds?: string[];
  hasExactRowScopeInitializer?: boolean;
}

export function mountTypedForm(options: MountTypedFormOptions) {
  const template = generatedActionTemplateFromCatalogEntry(options.entry);
  if (!template) throw new Error(`${options.entry.kind} has no generated template`);
  const onExecute = vi.fn<(request: GeneratedActionRequest, intent: 'preview' | 'run') => void>();
  const resolveParams = vi.fn(options.resolveParams);
  const estimateAction = options.estimateAction
    ? vi.fn<(request: GeneratedActionRequest) => Promise<RunEstimate>>(options.estimateAction)
    : undefined;
  const utils = render(<GeneratedActionForm
    catalogEntry={options.entry}
    actionTemplate={template}
    sheet={options.sheet}
    initialDraft={options.initialDraft}
    initialSourceColumn={options.initialSourceColumn}
    selectedRowIds={options.selectedRowIds}
    hasExactRowScopeInitializer={options.hasExactRowScopeInitializer}
    running={false}
    resolveParams={resolveParams}
    estimateAction={estimateAction}
    onExecute={onExecute}
    onClose={vi.fn()} />);
  return {
    ...utils,
    onExecute,
    resolveParams,
    estimateAction,
    /** The most recent typed request handed to onExecute. */
    lastRequest(): GeneratedActionRequest {
      const request = onExecute.mock.lastCall?.[0];
      if (!request) throw new Error('onExecute was not called');
      return request;
    },
  };
}
