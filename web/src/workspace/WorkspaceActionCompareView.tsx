import { memo, useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { isGeneratedActionCatalogEntry } from '../api/types';
import type { ActionComparisonBoundedValue } from '../api/actionComparison';
import type { HttpSelectorChoicesQuery, SelectorChoice } from '../api/selectorChoices';
import { readDocumentTextResource, readDocumentTextWindow } from '../api/raw/blobText';
import { ApiError } from '../api/contractErrors';
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
import { SelectorField } from '../engine-selector/SelectorField';
import { selectorUpdates } from '../engine-selector/selectorUpdates';
import { ActionCompareTab, type ActionCompareVariantEditorRenderProps } from '../workbench/ActionCompareTab';
import type { ActionCompareStructuredValue } from '../workbench/ActionCompareGrid';
import {
  actionCompareSelectorField,
  actionCompareSelectorQuery,
  createActionComparePresentation,
} from '../workbench/actionComparePresentation';
import { ACTION_COMPARE_PREVIEW_UNAVAILABLE_MESSAGE } from '../workbench/actionCompareSession';
import { CompareDiscardDialog } from '../workbench/CompareDiscardDialog';
import { ActionCompareHistoryMenu } from './ActionCompareHistoryMenu';
import { loadFrozenColumnCount } from './gridColumnState';
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
  if (editor.modelOnly) {
    const field = actionCompareSelectorField(entry);
    const query = actionCompareSelectorQuery(entry, editor.draft);
    const modelField = Object.entries(entry.ui_hints.semantic_controls).find(([, control]) => control === 'model')?.[0];
    if (!field || !query) return <p role="status">Model selection is not available.</p>;
    return <SelectorField projectId={chromePreferences.projectId} label={editor.label} query={query}
      recentNamespace={`${chromePreferences.projectId ?? 'none'}:action:${entry.kind}:${field}`}
      disabled={!chromePreferences.projectId || editor.modelDisabled}
      onCurrentChoiceChange={(choice) => { if (choice?.label) editor.onModelLabelChange?.(choice.label); }}
      onSelect={(choice) => {
        const updates = selectorUpdates(field, modelField, choice);
        if (updates) editor.onDraftChange({ ...editor.draft, params: { ...editor.draft.params, ...updates } });
      }} />;
  }
  return <GeneratedActionParamsEditor projectId={chromePreferences.projectId} catalogEntry={entry}
    actionTemplate={template} sheet={sheet} draft={editor.draft} onDraftChange={editor.onDraftChange}
    lockedParams={editor.lockedParams} structureLocked={editor.structureLocked} lockReason={editor.lockReason}
    resolveParams={resolveParams} />;
}

export function PreviewValueReader({ value, previewId, onUnavailable }: {
  value: Extract<ActionComparisonBoundedValue, { kind: 'preview_artifact' }>;
  previewId: string | null;
  onUnavailable(previewId: string): void;
}) {
  const unavailable = useCallback((error: unknown): never => {
    if (error instanceof ApiError && error.status === 404) {
      if (previewId !== null) onUnavailable(previewId);
      throw new Error(ACTION_COMPARE_PREVIEW_UNAVAILABLE_MESSAGE);
    }
    throw error;
  }, [onUnavailable, previewId]);
  const loadWindow = useCallback(async (options: { start: number; signal: AbortSignal }) => {
    try {
      return await readDocumentTextWindow(value.href, options);
    } catch (error) {
      return unavailable(error);
    }
  }, [unavailable, value.href]);
  const copyFullValue = useCallback(async (signal: AbortSignal) => {
    try {
      return await readDocumentTextResource(value.href, { signal });
    } catch (error) {
      return unavailable(error);
    }
  }, [unavailable, value.href]);
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
  const [value, setValue] = useState<{
    value: ActionComparisonBoundedValue;
    previewId: string | null;
  } | null>(null);
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
  const visibleSourceCount = state.context.source_columns.filter(
    column => !state.hiddenColumnKeys.includes(`source:${column.name}`),
  ).length;
  const frozenColumnCount = loadFrozenColumnCount(
    model.project.id,
    String(state.context.sheet.id),
    visibleSourceCount,
  );
  return <>
    {entry && selectorField && state.variants.map((variant) => {
      const query = actionCompareSelectorQuery(entry, variant.draft);
      return query && <ActionCompareVariantChoiceOwner key={variant.id}
        projectId={chromePreferences.projectId} variantId={variant.id} query={query} publish={publishChoice} />;
    })}
    <ActionCompareTab projectId={model.project.id} session={session}
      evaluationApi={model.expectedAnswers.api} initialLens={model.initialLens} dataRevision={model.comparisonDataVersion + model.expectedAnswers.revision}
      rowScope={model.rowScope} rowScopeTotal={model.rowScopeTotal} onChangeRowScope={model.changeActionCompareRowScope} rowScopePending={model.rowScopePending}
      modelField={selectorField}
      onJoinExpected={() => {
        const run = model.expectedAnswers.runs.find(item => item.context.draft.action_id === state.context.draft.action_id && item.context.output_columns.some(output => state.context.output_columns.some(current => current.column_id === output.column_id)));
        model.expectedAnswers.openJoin(run ? `${run.anchor.runId}:${run.anchor.columnId}` : undefined);
      }}
      onExpectedAnswersChanged={() => { model.expectedAnswers.refresh(); model.invalidateProjectData(); void model.refreshSheets(); }}
      onShelved={model.closeActionCompare}
      rowHeight={model.rowHeight} wrapText={model.wrapText} frozenColumnCount={frozenColumnCount}
      onUseSettings={model.useActionCompareSettings}
      labelForDraft={presentation?.labelForDraft}
      describeVariantChanges={presentation?.describeVariantChanges}
      renderVariantEditor={editor => <VariantEditor editor={editor} sheetId={state.context.sheet.id} />}
      onOpenBoundedValue={({ value: nextValue, previewId }) => {
        setStructuredValue(null);
        setValue({ value: nextValue, previewId });
      }}
      onOpenStructuredValue={(nextValue) => { setValue(null); setStructuredValue(nextValue); }} />
    {model.actionCompareCloseWarn && <CompareDiscardDialog title="Discard this comparison?"
      description="The comparison settings and preview results will be discarded."
      testId="action-compare" onCancel={model.cancelActionCompareClose} onDiscard={model.closeActionCompare} />}
    {model.actionComparePendingTarget && <CompareDiscardDialog title="Start a new comparison?"
      description="Starting another comparison discards these settings and preview results."
      testId="action-compare-retarget" onCancel={model.cancelActionCompareRetarget} onDiscard={model.confirmActionCompareRetarget} />}
    {value && <Modal title="Full value" size="wide" onClose={() => setValue(null)} closeLabel="Close full value">
      {value.value.kind === 'current_cell' ? <LargeCellValue projectId={model.project.id} rowId={String(value.value.row_id)}
        columnId={String(value.value.column_id)} descriptor={value.value} />
        : <PreviewValueReader value={value.value} previewId={value.previewId}
          onUnavailable={session.markPreviewUnavailable} />}
    </Modal>}
    {structuredValue && <Modal title={`${structuredValue.column.name} · sample row ${structuredValue.row.index + 1}`}
      size="wide" onClose={() => setStructuredValue(null)} closeLabel="Close full value">
      <FieldValue col={structuredValue.column} columns={[structuredValue.column]} row={structuredValue.row}
        sheetId={String(state.context.sheet.id)} value={structuredValue.row.cells[structuredValue.column.id] ?? null} />
    </Modal>}
  </>;
}

export function WorkspaceActionCompareHistoryMenu() {
  const model = useMainViewModel();
  const target = model.actionCompareHistoryMenuTarget;
  return target && <ActionCompareHistoryMenu anchor={target.runAnchor} trigger={target.trigger}
    onChoose={chosen => {
      model.closeActionCompareHistory();
      void model.restoreActionRunSettings(chosen);
    }}
    onClose={model.closeActionCompareHistory} />;
}
