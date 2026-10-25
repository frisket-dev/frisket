import { memo, useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { isGeneratedActionCatalogEntry } from '../api/types';
import type { ActionComparisonBoundedValue } from '../api/actionComparison';
import type { HttpSelectorChoicesQuery, SelectorChoice } from '../api/selectorChoices';
import { readDocumentTextResource, readDocumentTextWindow } from '../api/raw/blobText';
import { useActionCatalogHandle } from '../bind/useActionCatalogHandle';
import { useProjectDataResource } from '../bind/useRouteHandle';
import { useSelector } from '../bind/useSelector';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { BoundedTextReader } from '../components/BoundedTextReader';
import { LargeCellValue } from '../components/LargeCellValue';
import { Modal } from '../components/Modal';
import { FieldValue } from '../components/rowInspection/FieldValue';
import { GeneratedActionParamsEditor } from '../components/action-panel/GeneratedActionForm';
import { currentSelectorChoice, selectorQueryKey } from '../engine-selector/selectorProjection';
import { useSelectorChoices, type SelectorChoicesLoader } from '../engine-selector/useSelectorChoices';
import { ActionCompareTab, type ActionCompareVariantEditorRenderProps } from '../workbench/ActionCompareTab';
import type { ActionCompareStructuredValue } from '../workbench/ActionCompareGrid';
import {
  actionCompareSelectorField,
  actionCompareSelectorQuery,
  createActionComparePresentation,
} from '../workbench/actionComparePresentation';
import { CompareDiscardDialog } from '../workbench/CompareDiscardDialog';
import { ActionCompareHistoryDialog } from './ActionCompareHistoryDialog';
import { useMainViewModel } from './WorkspaceContext';

export const ActionCompareVariantChoiceOwner = memo(function ActionCompareVariantChoiceOwner({
  projectId, variantId, query, publish, load,
}: {
  projectId: string | null | undefined;
  variantId: string;
  query: HttpSelectorChoicesQuery;
  publish(variantId: string, choice: SelectorChoice | null): void;
  load?: SelectorChoicesLoader;
}) {
  const state = useSelectorChoices({
    projectId,
    query,
    queryKey: selectorQueryKey,
    ...(load ? { load } : {}),
  });
  const choice = currentSelectorChoice(state);
  useEffect(() => publish(variantId, choice), [choice, publish, variantId]);
  return null;
});

function VariantEditor({ editor, sheetId }: { editor: ActionCompareVariantEditorRenderProps; sheetId: number }) {
  const { projectApi, chromePreferences } = useWorkspaceStores();
  const catalog = useActionCatalogHandle();
  const snapshot = useSelector(catalog.store, state => state);
  const project = useProjectDataResource();
  const sheet = useSelector(project.store, state => state.sheets.find(sheet => sheet.id === String(sheetId)));
  const resolveParams = useCallback((request: Parameters<typeof projectApi.resolveActionParams>[0]) => projectApi.resolveActionParams(request), [projectApi]);
  const entry = snapshot.catalog?.actions.find(entry => entry.kind === editor.draft.action_id);
  const template = snapshot.resolvedTemplates.find(template => template.kind === editor.draft.action_id);
  if (!entry || !isGeneratedActionCatalogEntry(entry) || !template || !sheet) {
    return <p role="status">These action settings are not available.</p>;
  }
  return <GeneratedActionParamsEditor projectId={chromePreferences.projectId} catalogEntry={entry}
    actionTemplate={template} sheet={sheet} draft={editor.draft} onDraftChange={editor.onDraftChange}
    lockedParams={editor.lockedParams} structureLocked={editor.structureLocked} lockReason={editor.lockReason}
    resolveParams={resolveParams} />;
}

function PreviewValueReader({ value }: { value: Extract<ActionComparisonBoundedValue, { kind: 'preview_artifact' }> }) {
  const loadWindow = useCallback((options: { start: number; signal: AbortSignal }) => readDocumentTextWindow(value.href, options), [value.href]);
  const copyFullValue = useCallback((signal: AbortSignal) => readDocumentTextResource(value.href, { signal }), [value.href]);
  return <BoundedTextReader coordinate="bytes" loadWindow={loadWindow} downloadUrl={value.href} copyFullValue={copyFullValue} />;
}

/** Mounted for the workspace lifetime so hidden comparisons keep their scratch
 * results. Shared action forms and bounded readers own their normal behavior. */
export function WorkspaceActionCompareView() {
  const model = useMainViewModel();
  const { actionCompareSession: session } = model;
  if (!session) return null;
  return <ActionCompareContents key={session.getSnapshot().context.sample_fingerprint} />;
}

function ActionCompareContents() {
  const model = useMainViewModel();
  const session = model.actionCompareSession!;
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot, session.getSnapshot);
  const { chromePreferences } = useWorkspaceStores();
  const catalog = useActionCatalogHandle();
  const catalogSnapshot = useSelector(catalog.store, current => current);
  const [value, setValue] = useState<ActionComparisonBoundedValue | null>(null);
  const [variantChoices, setVariantChoices] = useState<Record<string, SelectorChoice | null>>({});
  const entry = catalogSnapshot.catalog?.actions.find(candidate => candidate.kind === state.context.draft.action_id);
  const selectorField = entry ? actionCompareSelectorField(entry) : null;
  const publishChoice = useCallback((variantId: string, choice: SelectorChoice | null) => {
    setVariantChoices(current => current[variantId] === choice ? current : { ...current, [variantId]: choice });
  }, []);
  const selectorChoices = useMemo(() => {
    if (!selectorField) return undefined;
    const choices = state.variants.flatMap(variant => variantChoices[variant.id] ? [variantChoices[variant.id]!] : []);
    return choices.length ? { [selectorField]: choices } : undefined;
  }, [selectorField, state.variants, variantChoices]);
  const presentation = useMemo(() => entry ? createActionComparePresentation(entry, {
    baselineMode: state.context.baseline_mode,
    lockedParams: state.context.locked_params,
    selectorChoices,
  }) : null, [entry, selectorChoices, state.context.baseline_mode, state.context.locked_params]);
  const [structuredValue, setStructuredValue] = useState<ActionCompareStructuredValue | null>(null);
  return <>
    {entry && selectorField && state.variants.map((variant) => {
      const query = actionCompareSelectorQuery(entry, variant.draft);
      return query && <ActionCompareVariantChoiceOwner key={variant.id}
        projectId={chromePreferences.projectId} variantId={variant.id} query={query} publish={publishChoice} />;
    })}
    <ActionCompareTab session={session} onUseSettings={model.useActionCompareSettings}
      labelForDraft={presentation?.labelForDraft}
      describeVariantChanges={presentation?.describeVariantChanges}
      renderVariantEditor={editor => <VariantEditor editor={editor} sheetId={state.context.sheet.id} />}
      onOpenBoundedValue={({ value: nextValue }) => { setStructuredValue(null); setValue(nextValue); }}
      onOpenStructuredValue={(nextValue) => { setValue(null); setStructuredValue(nextValue); }}
      onRefresh={model.refreshActionCompare} />
    {model.actionCompareCloseWarn && <CompareDiscardDialog title="Discard this comparison?"
      description="The comparison settings and preview results will be discarded."
      testId="action-compare" onCancel={model.cancelActionCompareClose} onDiscard={model.closeActionCompare} />}
    {model.actionComparePendingTarget && <CompareDiscardDialog title="Start a new comparison?"
      description="Starting another comparison discards these settings and preview results."
      testId="action-compare-retarget" onCancel={model.cancelActionCompareRetarget} onDiscard={model.confirmActionCompareRetarget} />}
    {value && <Modal title="Full value" size="wide" onClose={() => setValue(null)} closeLabel="Close full value">
      {value.kind === 'current_cell' ? <LargeCellValue projectId={model.project.id} rowId={String(value.row_id)}
        columnId={String(value.column_id)} descriptor={value} /> : <PreviewValueReader value={value} />}
    </Modal>}
    {structuredValue && <Modal title={`${structuredValue.column.name} · row ${structuredValue.row.index + 1}`}
      size="wide" onClose={() => setStructuredValue(null)} closeLabel="Close full value">
      <FieldValue col={structuredValue.column} columns={[structuredValue.column]} row={structuredValue.row}
        sheetId={String(state.context.sheet.id)} value={structuredValue.row.cells[structuredValue.column.id] ?? null} />
    </Modal>}
  </>;
}

export function WorkspaceActionCompareHistory() {
  const model = useMainViewModel();
  const anchor = model.actionCompareHistoryAnchor;
  return anchor && <ActionCompareHistoryDialog anchor={anchor}
    onChoose={chosen => { model.setActionCompareHistoryAnchor(null); void model.editActionRun(chosen); }}
    onClose={() => model.setActionCompareHistoryAnchor(null)} />;
}
