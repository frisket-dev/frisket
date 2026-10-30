import '../workbench/contributions.css';
import './WorkspaceChrome.css';
import { memo, useEffect, useMemo } from 'react';
import { MENTIONS_EXTRACT_ACTION_KIND } from '../components/mentionsPanelModel';
import { ImportWorkspaceDialog } from '../components/importWorkspace/ImportWorkspaceDialog';
import { SampleProjectOnboarding } from '../workbench/SampleProjectOnboarding';
import { ChromeBar } from '../workbench/ChromeBar';
import { useWalkthrough } from '../walkthrough/context';
import { CompareMenu } from '../workbench/CompareMenu';
import { ActRibbon } from '../workbench/ActRibbon';
import { ActMenuBar } from '../workbench/ActMenuBar';
import { ProjectExportModals, ProjectMenu } from '../components/TopNav';
import { ProjectAskDock } from '../components/ProjectAskDock';
import type { AskScope } from '../api/projectQA';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useChromeHandle } from '../bind/useChromeHandle';
import { useActSurfaceHandle } from '../bind/useActSurfaceHandle';
import { useActionCatalogHandle } from '../bind/useActionCatalogHandle';
import { useSelector } from '../bind/useSelector';
import { openAskProposalTransition } from '../state/workspaceTransitions';
import { useCurrentSheet, useWorkspaceShell, useWorkspaceAct, useRenderCount } from './WorkspaceContext';

export function WorkspaceAskRegion() {
  const { sheet, sheets } = useCurrentSheet();
  const { chrome, selection, detail } = useWorkspaceStores();
  const { selectSheet, askNavigation } = useWorkspaceShell();
  const { launchActionFromSurface } = useWorkspaceAct();
  const open = useSelector(chrome.store, (s) => s.askOpen);
  const selected = useSelector(selection.store, (s) => s.selectedRows);
  const scope = useMemo<AskScope>(() => {
    if (!sheet) return { kind: 'project' };
    const rows = selected.sheetId === sheet.id ? selected.rowIds.map(Number) : [];
    return { kind: 'sources', sources: rows.length
      ? [{ kind: 'rows', sheet_id: Number(sheet.id), row_ids: rows }]
      : [{ kind: 'sheet', sheet_id: Number(sheet.id) }] };
  }, [sheet, selected]);
  return open ? <ProjectAskDock onOpenSource={askNavigation.open} onOpenAction={launchActionFromSurface} initialScope={scope} sheets={sheets} onClose={chrome.closeAsk} onInspectProposal={(title, spec) => {
    if (spec.scope.kind === 'sheet_rows') selectSheet(String(spec.scope.sheet_id));
    openAskProposalTransition({ chrome, detail }, { seq: Date.now(), title, spec });
  }} /> : null;
}

export const WorkspaceChromeBarRegion = memo(function WorkspaceChromeBarRegion() {
  useRenderCount('chromeBar');
  const { project } = useWorkspaceShell();
  const chromeHandle = useChromeHandle();
  const askOpen = useSelector(chromeHandle.store, (state) => state.askOpen);
  const walkthrough = useWalkthrough();
  const openCommandPalette = chromeHandle.openCommandPalette;
  const toggleAsk = chromeHandle.toggleAsk;
  return (
    <>
      <SampleProjectOnboarding project={project} />
      <ChromeBar
        project={project}
        projectMenu={<WorkspaceProjectMenu />}
        onOpenCommandPalette={openCommandPalette}
        askOpen={askOpen}
        onToggleAsk={toggleAsk}
        walkthroughActive={walkthrough.active}
        walkthroughCanResume={walkthrough.canResume}
        walkthroughGuideSeen={walkthrough.guideSeen}
        walkthroughGuideEmphasized={walkthrough.guideEmphasized}
        walkthroughGuideHintVisible={walkthrough.guideHintVisible}
        onDismissGuideHint={walkthrough.dismissGuideHint}
        onOpenWalkthrough={walkthrough.openWalkthroughChooser}
        onResumeWalkthrough={walkthrough.resumeWalkthrough}
      />
      <WorkspaceProjectExportModals />
    </>
  );
});

function useWorkspaceExportContext() {
  const { project, projectApi } = useWorkspaceShell();
  const { sheet, sheets } = useCurrentSheet();
  const gridView = useGridViewHandle();
  const filter = useSelector(gridView.store, (state) => state.applied.filter);
  const sort = useSelector(gridView.store, (state) => state.applied.sort);
  const scopeRowIds = useSelector(gridView.store, (state) => state.applied.scopeRowIds ?? null);
  const currentSheetExportOptions = filter || sort || scopeRowIds !== null
    ? { filter, sort, scopeRowIds }
    : null;
  return { project, projectApi, currentSheet: sheet ?? null, sheets, currentSheetExportOptions };
}

function WorkspaceProjectMenu() {
  const context = useWorkspaceExportContext();
  const actSurface = useActSurfaceHandle();
  const targets = useSelector(actSurface.store, (state) => state.actExportTargets);
  return <ProjectMenu {...context} catalogExportTargets={targets} />;
}

function WorkspaceProjectExportModals() {
  const context = useWorkspaceExportContext();
  const actSurface = useActSurfaceHandle();
  const modal = useSelector(actSurface.store, (state) => state.actExportModal);
  const actionCatalog = useActionCatalogHandle();
  const columnTablesExportEntry = useSelector(actionCatalog.store, (state) => (
    modal === 'column_tables' && state.status === 'ready'
      ? state.catalog?.actions.find((entry) => entry.kind === 'export.column_tables')
      : undefined
  ));
  return <ProjectExportModals {...context} modal={modal}
    columnTablesExportEntry={columnTablesExportEntry} onClose={actSurface.closeActExportModal} />;
}

export const WorkspaceActRegion = memo(function WorkspaceActRegion() {
  useRenderCount('act');
  const {
    runActCommand,
    revealPluginLauncher,
    onImported,
    refreshSheets,
    invalidateProjectData,
    showError,
    setRibbonMode,
    setActiveRibbonTab,
  } = useWorkspaceShell();
  const { actRibbonTabs, launchActionFromSurface, runActionFromSurface, expectedAnswers, compareActionRun } = useWorkspaceAct();
  const chrome = useChromeHandle();
  const ribbonMode = useSelector(chrome.store, (s) => s.ribbonMode);
  const activeRibbonTab = useSelector(chrome.store, (s) => s.activeRibbonTab);
  const actSurface = useActSurfaceHandle();
  const actionCatalog = useActionCatalogHandle();
  const catalogStatus = useSelector(actionCatalog.store, (state) => state.status);
  const importDialogOpen = useSelector(actSurface.store, (s) => s.importDialogOpen);
  const importDialogEntryMode = useSelector(actSurface.store, (s) => s.importDialogEntryMode);
  const importDialogTableFile = useSelector(actSurface.store, (s) => s.importDialogTableFile);
  const importDialogTableRerunDraftId = useSelector(
    actSurface.store,
    (s) => s.importDialogTableRerunDraftId,
  );
  const importDialogFiles = useSelector(actSurface.store, (s) => s.importDialogFiles);
  const importPreparationBatchId = useSelector(
    actSurface.store,
    (s) => s.importPreparationBatchId,
  );
  const nerTemplate = useSelector(actionCatalog.store, (state) =>
    state.status === 'ready'
      ? state.resolvedTemplates.find((template) => template.kind === 'map.ner')
      : undefined,
  );
  const ftmImportEnabled = useSelector(actionCatalog.store, (state) =>
    state.status === 'ready'
      && state.resolvedTemplates.some((template) => template.kind === 'frisket.ftm.ftm_import'),
  );
  const closeImportDialog = actSurface.closeImportDialog;
  useEffect(() => {
    // Command tabs appear before catalog-backed tabs; keep the saved selection
    // until the catalog has settled so loading cannot overwrite it.
    if (catalogStatus === 'idle' || catalogStatus === 'loading') return;
    if (actRibbonTabs.length === 0 || actRibbonTabs.some((tab) => tab.id === activeRibbonTab)) {
      return;
    }
    const fallbackTab = actRibbonTabs.find((tab) => tab.id === 'analyze') ?? actRibbonTabs[0];
    if (fallbackTab) setActiveRibbonTab(fallbackTab.id);
  }, [catalogStatus, actRibbonTabs, activeRibbonTab, setActiveRibbonTab]);
  return (
    <section
      className="workbench-region workbench-act"
      data-testid="workbench-region-act"
      aria-label="Act region"
      data-ribbon-mode={ribbonMode}
    >
      {ribbonMode === 'ribbon' ? (
        <ActRibbon
          tabs={actRibbonTabs}
          activeTabId={activeRibbonTab}
          onSelectTab={setActiveRibbonTab}
          onRunAction={launchActionFromSurface}
          onCommand={runActCommand}
          onLaunchContribution={revealPluginLauncher}
          compareSlot={(tab) => <CompareMenu media={tab === 'media'} runs={expectedAnswers.runChoices} actions={expectedAnswers.actionChoices} onCompareRun={compareActionRun} onCompareAction={(kind) => { if (kind === 'media.ocr') runActCommand('ocr-compare'); else if (kind === 'media.transcribe') runActCommand('transcribe-compare'); else if (kind === 'map.find_topic_sections') runActCommand('topic-compare'); else launchActionFromSurface(kind); }} onJoinExpected={() => expectedAnswers.openJoin()} />}
          onCollapse={() => setRibbonMode('menu')}
        />
      ) : (
        <ActMenuBar
          tabs={actRibbonTabs}
          onRunAction={launchActionFromSurface}
          onCommand={runActCommand}
          onLaunchContribution={revealPluginLauncher}
          compareSlot={(tab, close) => <CompareMenu inline onDismiss={close} media={tab === 'media'} runs={expectedAnswers.runChoices} actions={expectedAnswers.actionChoices} onCompareRun={compareActionRun} onCompareAction={(kind) => { if (kind === 'media.ocr') runActCommand('ocr-compare'); else if (kind === 'media.transcribe') runActCommand('transcribe-compare'); else if (kind === 'map.find_topic_sections') runActCommand('topic-compare'); else launchActionFromSurface(kind); }} onJoinExpected={() => expectedAnswers.openJoin()} />}
          onExpandRibbon={() => setRibbonMode('ribbon')}
        />
      )}
      <ImportWorkspaceDialog
        open={importDialogOpen}
        entryMode={importDialogEntryMode}
        initialTableFile={importDialogTableFile}
        initialTableRerunDraftId={importDialogTableRerunDraftId}
        initialFiles={importDialogFiles}
        initialPreparationBatchId={importPreparationBatchId}
        onClose={closeImportDialog}
        onImported={expectedAnswers.join ? () => { invalidateProjectData(); void refreshSheets(); expectedAnswers.refresh(); } : onImported}
        onError={showError}
        onLaunchDownload={runActionFromSurface}
        onExtractEntities={() => runActionFromSurface(MENTIONS_EXTRACT_ACTION_KIND)}
        nerTemplate={nerTemplate}
        ftmImportEnabled={ftmImportEnabled}
      />
    </section>
  );
});
