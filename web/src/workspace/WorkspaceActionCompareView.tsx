import { useCallback, useState, useSyncExternalStore } from 'react';
import { isGeneratedActionCatalogEntry } from '../api/types';
import type { ActionComparisonBoundedValue } from '../api/actionComparison';
import { readDocumentTextResource, readDocumentTextWindow } from '../api/raw/blobText';
import { useActionCatalogHandle } from '../bind/useActionCatalogHandle';
import { useProjectDataResource } from '../bind/useRouteHandle';
import { useSelector } from '../bind/useSelector';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { BoundedTextReader } from '../components/BoundedTextReader';
import { LargeCellValue } from '../components/LargeCellValue';
import { Modal } from '../components/Modal';
import { GeneratedActionParamsEditor } from '../components/action-panel/GeneratedActionForm';
import { ActionCompareTab, type ActionCompareVariantEditorRenderProps } from '../workbench/ActionCompareTab';
import { CompareDiscardDialog } from '../workbench/CompareDiscardDialog';
import { ActionCompareHistoryDialog } from './ActionCompareHistoryDialog';
import { useMainViewModel } from './WorkspaceContext';

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
  const [value, setValue] = useState<ActionComparisonBoundedValue | null>(null);
  return <>
    <ActionCompareTab session={session} onUseSettings={model.useActionCompareSettings}
      renderVariantEditor={editor => <VariantEditor editor={editor} sheetId={state.context.sheet.id} />}
      onOpenBoundedValue={({ value }) => setValue(value)} onRefresh={model.refreshActionCompare} />
    {model.actionCompareCloseWarn && <CompareDiscardDialog title="Discard this comparison?"
      description="The comparison settings and preview results will be discarded. Your table will not change."
      testId="action-compare" onCancel={model.cancelActionCompareClose} onDiscard={model.closeActionCompare} />}
    {model.actionComparePendingTarget && <CompareDiscardDialog title="Start a new comparison?"
      description="Starting another comparison discards these settings and preview results. Your table will not change."
      testId="action-compare-retarget" onCancel={model.cancelActionCompareRetarget} onDiscard={model.confirmActionCompareRetarget} />}
    {value && <Modal title="Full value" size="wide" onClose={() => setValue(null)} closeLabel="Close full value">
      {value.kind === 'current_cell' ? <LargeCellValue projectId={model.project.id} rowId={String(value.row_id)}
        columnId={String(value.column_id)} descriptor={value} /> : <PreviewValueReader value={value} />}
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
