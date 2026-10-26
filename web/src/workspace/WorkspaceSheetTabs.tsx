import './WorkspaceSheetTabs.css';
import { memo, useCallback, useRef, useState } from 'react';
import { MenuPop } from '../components/MenuPop';
import { ChevronRight, GitCompareArrows, GitFork, Images, Info, MapPin, Network, Rows3, AudioLines, Languages, ScanText, Sheet as SheetGlyph, Trash2, X } from 'lucide-react';
import { useNativePopover } from '../hooks/useNativePopover';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';
import { useSheetTabsOverflow } from '../workbench/useSheetTabsOverflow';
import { ConfirmDeleteSheetModal } from '../components/ConfirmDeleteSheetModal';
import { AddSheetButton } from '../components/TabStripButtons';
import { staleSheetsDeepestFirst } from '../workbench/lineageStale';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { previewViewForSheet } from '../state/previewViewStore';
import { useChromeHandle } from '../bind/useChromeHandle';
import { useActSurfaceHandle } from '../bind/useActSurfaceHandle';
import { useWorkViewHandle } from '../bind/useWorkViewHandle';
import { useCompareViewHandle } from '../bind/useCompareViewHandle';
import { usePreviewViewHandle } from '../bind/usePreviewViewHandle';
import { useSelector } from '../bind/useSelector';
import type { CompareKind } from '../state/compareViewStore';
import { focusCompareTabTransition } from '../state/workspaceTransitions';
import { useWorkspaceShell, useCurrentSheet } from './WorkspaceContext';
import { SheetStalePill, SheetInfoPopover } from './SheetInfoPopover';

const COMPARE_TABS = [
  { kind: 'ocr', label: 'OCR compare', Icon: ScanText },
  { kind: 'transcribe', label: 'Transcribe compare', Icon: AudioLines },
  { kind: 'translate', label: 'Translate compare', Icon: Languages },
  { kind: 'topic', label: 'Topic compare', Icon: Rows3 },
  { kind: 'action', label: 'Action compare', Icon: GitCompareArrows },
] as const satisfies readonly { kind: CompareKind; label: string; Icon: typeof ScanText }[];

export const WorkspaceMainViewTabs = memo(function WorkspaceMainViewTabs() {
  const {
    onMainViewTabKeyDown,
    closePreviewView,
    activePromotedView,
    openPromotedTab,
    closePromotedTab,
    selectSheetTab,
    selectSheet,
    refreshSheets,
  } = useWorkspaceShell();
  const { sheet, sheets } = useCurrentSheet();
  const actSurface = useActSurfaceHandle();
  const openImportDialog = actSurface.openImportDialog;
  const workView = useWorkViewHandle();
  const compareView = useCompareViewHandle();
  const previewViewHandle = usePreviewViewHandle();
  const activePreviewView = useSelector(previewViewHandle.store, (s) =>
    previewViewForSheet(s.previewView, sheet?.id),
  );
  const compareTabs = useSelector(compareView.store, (state) => state);
  const compareActive = Object.values(compareTabs).some((tab) => tab.active);
  const chrome = useChromeHandle();
  const { projectApi } = useWorkspaceStores();

  const promotedViews = useSelector(chrome.store, (s) => s.promotedViews);
  const setActiveBottomDockTab = chrome.setActiveBottomDockTab;

  const [infoSheetId, setInfoSheetId] = useState<string | null>(null);
  const infoSheet = sheets.find((s) => s.id === infoSheetId) ?? null;
  const [deleteSheetId, setDeleteSheetId] = useState<string | null>(null);
  const [deleteSheetBusy, setDeleteSheetBusy] = useState(false);
  const [deleteSheetError, setDeleteSheetError] = useState<string | null>(null);
  const deleteSheet = sheets.find((candidate) => candidate.id === deleteSheetId) ?? null;
  const deleteSheetDependents = deleteSheet
    ? (deleteSheet.dependentSheetIds ?? []).flatMap((id) => {
        const dependent = sheets.find((candidate) => candidate.id === id);
        return dependent ? [dependent.name] : [];
      })
    : [];

  const confirmDeleteSheet = useCallback(async () => {
    if (!deleteSheet || deleteSheetBusy || deleteSheetDependents.length > 0) return;
    setDeleteSheetBusy(true);
    setDeleteSheetError(null);
    try {
      await projectApi.deleteSheet(deleteSheet.id);
      setDeleteSheetId(null);
      await refreshSheets();
    } catch (error) {
      setDeleteSheetError(error instanceof Error ? error.message : String(error));
    } finally {
      setDeleteSheetBusy(false);
    }
  }, [deleteSheet, deleteSheetBusy, deleteSheetDependents.length, projectApi, refreshSheets]);

  // Preserve deepest-first cascade order.
  const staleDeepestFirst = staleSheetsDeepestFirst(sheets);
  const [moreTabsOpen, setMoreTabsOpen] = useState(false);
  const moreTabsTriggerRef = useRef<HTMLButtonElement>(null);
  const moreTabsMenuRef = useRef<HTMLDivElement>(null);
  const hasActiveSheet = Boolean(
    sheet
    && !activePromotedView
    && !compareActive,
  );
  const { sheetTabsRef, measureRef: sheetTabsMeasureRef, visibleSheets, overflowSheets } = useSheetTabsOverflow(
    sheets,
    sheet,
    hasActiveSheet,
  );
  if (moreTabsOpen && overflowSheets.length === 0) setMoreTabsOpen(false);
  useNativePopover(moreTabsMenuRef, () => setMoreTabsOpen(false), {
    enabled: moreTabsOpen,
    ignoreSelector: '[data-testid="workbench-mainView-more-tabs"]',
  });
  const moreTabsPosition = useAnchoredPosition(moreTabsTriggerRef, {
    enabled: moreTabsOpen,
    align: 'right',
    width: 220,
    gap: 4,
  });

  return (
    <>
      <div
        className="workbench-mainView-tabs"
        data-testid="workbench-mainView-tabs"
        data-host="mainView"
        data-mode="tab"
      >
        <div className="workbench-mainView-tab-list" role="tablist" aria-label="Open work surfaces">
          <div className="workbench-mainView-sheetTabs" ref={sheetTabsRef}>
            <div className="workbench-mainView-tab-measure" ref={sheetTabsMeasureRef} aria-hidden>
              {sheets.map((tabSheet) => (
                <span key={tabSheet.id} className={`workbench-mainView-tab${tabSheet.id === sheet?.id && hasActiveSheet ? ' active' : ''}`}>
                  {tabSheet.parent ? <GitFork size={13} className="workbench-mainView-tab-glyph" /> : <SheetGlyph size={13} className="workbench-mainView-tab-glyph" />}
                  <span className="workbench-mainView-tab-name">{tabSheet.name}</span>
                  {tabSheet.parent && tabSheet.refreshable && tabSheet.syncState && (
                    <span className="workbench-mainView-tab-syncDot" />
                  )}
                  {tabSheet.id === sheet?.id && hasActiveSheet && <>
                    <span className="workbench-mainView-tab-rowcount">{tabSheet.rowCount.toLocaleString()}</span>
                    {tabSheet.parent && <span className="workbench-mainView-tab-info"><Info size={12} /></span>}
                    <span className="workbench-mainView-tab-info"><Trash2 size={12} /></span>
                  </>}
                </span>
              ))}
            </div>
          {visibleSheets.map((tabSheet) => {
            const active =
              sheet?.id === tabSheet.id &&
              !activePromotedView &&
              !compareActive;
            const derived = Boolean(tabSheet.parent);
            return (
              <button
                key={tabSheet.id}
                type="button"
                role="tab"
                className={`workbench-mainView-tab${active ? ' active' : ''}`}
                aria-selected={active}
                data-active={active ? 'true' : undefined}
                data-derived={derived ? 'true' : undefined}
                data-testid={`workbench-mainView-tab-${tabSheet.id}`}
                data-contribution-id="frisket.core.view.grid"
                data-host="mainView"
                data-mode="tab"
                tabIndex={active ? 0 : -1}
                onClick={() => selectSheetTab(tabSheet.id)}
                onKeyDown={(event) => onMainViewTabKeyDown(event, tabSheet.id)}
              >
                {derived ? (
                  <GitFork
                    size={13}
                    className="workbench-mainView-tab-glyph"
                    data-testid={`workbench-mainView-tab-derived-${tabSheet.id}`}
                    aria-label="Derived sheet"
                  />
                ) : (
                  <SheetGlyph size={13} className="workbench-mainView-tab-glyph" aria-hidden />
                )}
                <span className="workbench-mainView-tab-name">{tabSheet.name}</span>
                {derived && tabSheet.refreshable && tabSheet.syncState && (
                  <span
                    className="workbench-mainView-tab-syncDot"
                    data-sync-state={tabSheet.syncState}
                    data-testid={`workbench-mainView-tab-syncDot-${tabSheet.id}`}
                    aria-label={
                      tabSheet.syncState === 'stale'
                        ? 'Source changed'
                        : 'Up to date'
                    }
                    title={
                      tabSheet.syncState === 'stale'
                        ? 'Source changed'
                        : 'Up to date'
                    }
                  />
                )}
                {active && (
                  <span
                    className="workbench-mainView-tab-rowcount"
                    data-testid={`workbench-mainView-tab-rowcount-${tabSheet.id}`}
                  >
                    {tabSheet.rowCount.toLocaleString()}
                  </span>
                )}
                {active && derived && (

                  // HTML forbids nested buttons.

                  <span
                    role="button"
                    tabIndex={0}
                    className="workbench-mainView-tab-info"
                    data-testid={`workbench-mainView-tab-info-${tabSheet.id}`}
                    aria-label="Sheet info"
                    title="Sheet info"
                    onClick={(event) => {
                      event.stopPropagation();
                      setInfoSheetId((cur) => (cur === tabSheet.id ? null : tabSheet.id));
                    }}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        event.stopPropagation();
                        setInfoSheetId((cur) => (cur === tabSheet.id ? null : tabSheet.id));
                      }
                    }}
                  >
                    <Info size={12} aria-hidden />
                  </span>
                )}
                {active && (
                  <span
                    role="button"
                    tabIndex={0}
                    className="workbench-mainView-tab-info"
                    data-testid={`workbench-mainView-tab-delete-${tabSheet.id}`}
                    aria-label={`Delete ${tabSheet.name}`}
                    title="Delete sheet"
                    onClick={(event) => {
                      event.stopPropagation();
                      setDeleteSheetError(null);
                      setDeleteSheetId(tabSheet.id);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        event.stopPropagation();
                        setDeleteSheetError(null);
                        setDeleteSheetId(tabSheet.id);
                      }
                    }}
                  >
                    <Trash2 size={12} aria-hidden />
                  </span>
                )}
              </button>
            );
          })}
            {overflowSheets.length > 0 && (
              <>
                <button
                  type="button"
                  ref={moreTabsTriggerRef}
                  className={`workbench-mainView-moreTabs${moreTabsOpen ? ' open' : ''}`}
                  data-testid="workbench-mainView-more-tabs"
                  aria-label="More tabs"
                  title="More tabs"
                  aria-haspopup="menu"
                  aria-expanded={moreTabsOpen}
                  onClick={() => setMoreTabsOpen((open) => !open)}
                >
                  <ChevronRight size={16} aria-hidden />
                </button>
                {moreTabsOpen && (
                  <MenuPop
                    ref={moreTabsMenuRef}
                    className="workbench-mainView-moreTabsMenu"
                    style={moreTabsPosition
                      ? { position: 'fixed', inset: 'auto', top: moreTabsPosition.top, bottom: moreTabsPosition.bottom, left: moreTabsPosition.left, width: moreTabsPosition.width, margin: 0 }
                      : { position: 'fixed', visibility: 'hidden' }}
                  >
                    {overflowSheets.map((tabSheet) => (
                      <button
                        key={tabSheet.id}
                        type="button"
                        role="menuitem"
                        className={`menu-item${tabSheet.id === sheet?.id ? ' current' : ''}`}
                        onClick={() => {
                          selectSheetTab(tabSheet.id);
                          setMoreTabsOpen(false);
                        }}
                      >
                        {tabSheet.parent ? <GitFork size={13} aria-hidden /> : <SheetGlyph size={13} aria-hidden />}
                        <span className="menu-item-name">{tabSheet.name}</span>
                      </button>
                    ))}
                  </MenuPop>
                )}
              </>
            )}
          </div>
          {activePreviewView && (
            <span
              className="workbench-mainView-tab workbench-mainView-previewTab active"
              role="tab"
              aria-selected="true"
              data-testid="workbench-mainView-previewTab"
            >
              <span className="preview-tab-glyph" aria-hidden />
              {activePreviewView.sheetId && sheet ? `${sheet.name} · ` : ''}{activePreviewView.actionName} Preview
              <button
                type="button"
                className="preview-tab-close"
                aria-label="Close preview"
                data-testid="preview-tab-close"
                onClick={(event) => {
                  event.stopPropagation();

                  closePreviewView();
                }}
              >
                <X size={13} />
              </button>
            </span>
          )}
          {promotedViews.map((view) => {
            const active = activePromotedView?.key === view.key;
            return (
              <span
                key={view.key}
                role="tab"
                tabIndex={0}
                className={`workbench-mainView-tab workbench-mainView-promotedTab${active ? ' active' : ''}`}
                aria-selected={active}
                data-active={active ? 'true' : undefined}
                data-testid="workbench-mainView-promotedTab"
                data-promoted-key={view.key}
                data-view-kind={view.kind}
                data-host="mainView"
                data-mode="tab"
                onClick={() => openPromotedTab(view)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    openPromotedTab(view);
                  }
                }}
              >
                {view.kind === 'map' ? (
                  <MapPin size={13} className="workbench-mainView-tab-glyph" aria-hidden />
                ) : view.kind === 'gallery' ? (
                  <Images size={13} className="workbench-mainView-tab-glyph" aria-hidden />
                ) : (
                  <Network size={13} className="workbench-mainView-tab-glyph" aria-hidden />
                )}
                <span className="workbench-mainView-tab-name workbench-promotedTab-label">
                  {view.label}
                </span>
                <button
                  type="button"
                  className="preview-tab-close"
                  aria-label={`Close ${view.label}`}
                  data-testid="workbench-mainView-promotedTab-close"
                  data-promoted-key={view.key}
                  onClick={(event) => {
                    event.stopPropagation();
                    closePromotedTab(view);
                  }}
                >
                  <X size={13} />
                </button>
              </span>
            );
          })}
          {COMPARE_TABS.map(({ kind, label, Icon }) => {
            const tab = compareTabs[kind];
            if (!tab.open) return null;
            const title = tab.session.label ?? label;
            const focus = () => focusCompareTabTransition(workView, compareView, kind);
            return (
              <span key={kind} role="tab" tabIndex={0}
                className={`workbench-mainView-tab workbench-mainView-promotedTab${tab.active ? ' active' : ''}`}
                aria-selected={tab.active} data-active={tab.active ? 'true' : undefined}
                data-testid={`${kind}-compare-maintab`} data-host="mainView" data-mode="tab"
                onClick={focus} onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    focus();
                  }
                }}>
                <Icon size={13} className="workbench-mainView-tab-glyph" aria-hidden />
                <span className="workbench-mainView-tab-name workbench-promotedTab-label">{title}</span>
                <button type="button" className="preview-tab-close" aria-label={`Close ${title}`}
                  data-testid={`${kind}-compare-maintab-close`} onClick={(event) => {
                    event.stopPropagation();
                    compareView.requestClose(kind);
                  }}><X size={13} /></button>
              </span>
            );
          })}
          <AddSheetButton onClick={openImportDialog} />
        </div>
        {staleDeepestFirst.length > 0 && (
          <SheetStalePill
            staleDeepestFirst={staleDeepestFirst}
            refreshSheets={refreshSheets}
          />
        )}
      </div>
      {deleteSheet && (
        <ConfirmDeleteSheetModal
          sheetName={deleteSheet.name}
          dependentSheetNames={deleteSheetDependents}
          busy={deleteSheetBusy}
          error={deleteSheetError}
          onConfirm={() => void confirmDeleteSheet()}
          onCancel={() => {
            if (!deleteSheetBusy) setDeleteSheetId(null);
          }}
        />
      )}
      {infoSheet && infoSheet.parent && (
        <SheetInfoPopover
          sheet={infoSheet}
          onClose={() => setInfoSheetId(null)}
          onNavigateParent={(parentId) => {
            setInfoSheetId(null);
            selectSheet(parentId);
          }}
          onViewLineage={() => {
            setInfoSheetId(null);
            setActiveBottomDockTab('lineage');
          }}
          refreshSheets={refreshSheets}
        />
      )}
    </>
  );
});
