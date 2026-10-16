import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Search, X } from 'lucide-react';
import { type SheetMeta } from '../api/open';
import type { ProjectSearchHitWire } from '../api/projectSearch';
import type { WorkbenchCommandEntry } from './commandRegistry';
import { usePaletteSearch, type PaletteSearchSeed } from './usePaletteSearch';
import {
  uniqueVisibilityTargetsByContribution,
  type WorkbenchVisibilityTarget,
} from './visibility';
import {
  PaletteCommandsSection,
  PaletteProductionSections,
} from './WorkbenchCommandPaletteSections';
import { PaletteSearchSection } from './WorkbenchCommandPaletteSearch';
import { useNativePopover } from '../hooks/useNativePopover';
import { topLayerPortalRoot } from '../topLayerPortal';
import { createSearchPreparation, listSearchPreparations } from '../api/searchPreparation';
import { SearchPreparationReceipt } from '../components/SearchPreparationReceipt';

const EMPTY_LAUNCHER_COMMANDS: PaletteLauncherCommand[] = [];

/** An action offered by the palette — BEST MATCH (pre-bound to a source column)
 *  or the general ACTIONS list. Activating one opens the configure-first drawer;
 *  it never runs the action directly. */
export interface PaletteActionItem {
  actionKind: string;
  name: string;
  /** Extra search terms matched alongside `name` (ActionTemplate.keywords) —
   *  keeps a renamed action findable by its retired display term. */
  keywords?: string[];
  sourceColumn?: string;
  columnType?: string;
}

/** A GO TO destination — a sheet, reached by name. Activating navigates. */
export interface PaletteGotoItem {
  sheetId: string;
  name: string;
}

/** A plugin launcher command (ex-activityRail): reveals a plugin panel/view's
 *  primary placement. Rendered in the Commands section alongside the
 *  first-party command entries. */
export interface PaletteLauncherCommand {
  contributionId: string;
  label: string;
  run: () => void;
}

function matchesQuery(haystack: string, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return q.split(/\s+/).every((token) => haystack.toLowerCase().includes(token));
}

// A flat, keyboard-navigable row across every filtered section — the
// "production work" sections (BEST MATCH / ACTIONS / GO TO / SEARCH) plus
// COMMANDS (first-party commands, plugin launchers, and hide/reveal
// visibility toggles). COMMANDS joined the flat nav once it became
// query-filterable — a filtered set the user can't arrow-key through would
// be a half-fix.
type NavRow =
  | { key: string; kind: 'action'; item: PaletteActionItem }
  | { key: string; kind: 'goto'; item: PaletteGotoItem }
  | { key: string; kind: 'command'; entry: WorkbenchCommandEntry }
  | { key: string; kind: 'launcher'; item: PaletteLauncherCommand }
  | { key: string; kind: 'visibility'; action: 'hide' | 'reveal'; target: WorkbenchVisibilityTarget }
  | { key: string; kind: 'search'; hit: ProjectSearchHitWire };

export function WorkbenchCommandPalette({
  onClose,
  commands,
  onHideContribution,
  onRevealContribution,
  lastAction,
  visibilityTargets,
  query,
  onQueryChange,
  bestMatchItems,
  actionItems,
  gotoItems,
  onLaunchAction,
  onNavigateSheet,
  searchSheets,
  activeSearchSheetId,
  searchSeed,
  onNavigateSearchHit,
  projectId,
  onOpenJobs,
  launcherCommands = EMPTY_LAUNCHER_COMMANDS,
}: {
  onClose: () => void;
  commands: WorkbenchCommandEntry[];
  launcherCommands?: PaletteLauncherCommand[];
  onHideContribution: (target: WorkbenchVisibilityTarget) => void;
  onRevealContribution: (target: WorkbenchVisibilityTarget) => void;
  lastAction: string;
  visibilityTargets: WorkbenchVisibilityTarget[];
  query: string;
  onQueryChange: (value: string) => void;
  bestMatchItems: PaletteActionItem[];
  actionItems: PaletteActionItem[];
  gotoItems: PaletteGotoItem[];
  onLaunchAction: (actionKind: string, sourceColumn?: string) => void;
  onNavigateSheet: (sheetId: string) => void;
  searchSheets: SheetMeta[];
  activeSearchSheetId?: string | number | null;
  onNavigateSearchHit: (hit: ProjectSearchHitWire) => void;
  searchSeed?: PaletteSearchSeed | null;
  projectId?: string;
  onOpenJobs?: () => void;
}) {
  const backdropRef = useRef<HTMLDivElement>(null);
  useNativePopover(backdropRef, onClose, { escape: false, outside: false });

  const hideTargets = uniqueVisibilityTargetsByContribution(
    visibilityTargets.filter(
      (target) => target.hideable && target.status !== 'hidden' && target.status !== 'missing',
    ),
  );
  const revealTargets = uniqueVisibilityTargetsByContribution(
    visibilityTargets.filter(
      (target) => target.hideable && target.status === 'hidden',
    ),
  );

  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Live text filtering (the spec's named production work): the query narrows
  // BEST MATCH / ACTIONS / GO TO as it is typed.
  const filteredBestMatch = useMemo(
    () => bestMatchItems.filter((item) => matchesQuery(
      `${item.name} ${(item.keywords ?? []).join(' ')} ${item.sourceColumn ?? ''}`,
      query,
    )),
    [bestMatchItems, query],
  );
  const filteredActions = useMemo(
    () => actionItems.filter((item) => matchesQuery(
      `${item.name} ${(item.keywords ?? []).join(' ')}`,
      query,
    )),
    [actionItems, query],
  );
  const filteredGoto = useMemo(
    () => gotoItems.filter((item) => matchesQuery(item.name, query)),
    [gotoItems, query],
  );

  // COMMANDS filtering — first-party command entries, plugin launchers, and
  // hide/reveal visibility labels, narrowed by the same query the sibling
  // sections above already use.
  const filteredCommands = useMemo(
    () => commands.filter((entry) => matchesQuery(entry.descriptor.title, query)),
    [commands, query],
  );
  const filteredLauncherCommands = useMemo(
    () => launcherCommands.filter((item) => matchesQuery(item.label, query)),
    [launcherCommands, query],
  );
  const filteredHideTargets = useMemo(
    () =>
      hideTargets.filter((target) =>
        matchesQuery(`${target.locationLabel} ${target.title}`, query),
      ),
    [hideTargets, query],
  );
  const filteredRevealTargets = useMemo(
    () =>
      revealTargets.filter((target) =>
        matchesQuery(`${target.locationLabel} ${target.title}`, query),
      ),
    [revealTargets, query],
  );
  const hasCommandsSectionResults =
    filteredCommands.length > 0 ||
    filteredLauncherCommands.length > 0 ||
    filteredHideTargets.length > 0 ||
    filteredRevealTargets.length > 0;
  // SEARCH section — project-wide FTS row search reusing the same query API
  // + snippet rendering as the sidebar Search modal.
  const {
    mode,
    setMode,
    rerank,
    setRerank,
    groups,
    coverage,
    searchError,
    canWatch,
    watchBusy,
    watchStatus,
    createSearchWatch,
    namedIndexes,
    namedIndexId,
    setNamedIndexId,
    namedIndexError,
    canSaveLens,
    saveBusy,
    saveStatus,
    saveSearchLens,
    sheetName,
    anchor,
    anchorLabel,
    clearAnchor,
    findSimilar,
    setupBusy,
    setupError,
    setupOperation,
    startSetup,
    finishSetup,
  } = usePaletteSearch(
    query,
    searchSheets,
    activeSearchSheetId,
    onClose,
    searchSeed,
  );
  const anchored = anchor !== null;
  const [preparationBatchId, setPreparationBatchId] = useState<string | null>(null);
  const [preparationBusy, setPreparationBusy] = useState(false);
  const [preparationError, setPreparationError] = useState<string | null>(null);
  useEffect(() => {
    if (!projectId) return undefined;
    const controller = new AbortController();
    void listSearchPreparations(projectId, { signal: controller.signal })
      .then(({ batches }) => {
        const awaitingApproval = batches.find((batch) => batch.status === 'needs_consent');
        if (awaitingApproval) {
          setPreparationBatchId((current) => current ?? awaitingApproval.id);
        }
      })
      .catch((error: Error) => {
        if (error.name !== 'AbortError') setPreparationError(error.message);
      });
    return () => controller.abort();
  }, [projectId]);
  const prepareSearchGap = (sheetId: number) => {
    if (!projectId || !Number.isInteger(sheetId) || sheetId <= 0 || preparationBusy) return;
    setPreparationBusy(true);
    setPreparationError(null);
    void createSearchPreparation(projectId, { sheet_id: sheetId })
      .then((batch) => setPreparationBatchId(batch.id))
      .catch((error) => setPreparationError(error instanceof Error ? error.message : String(error)))
      .finally(() => setPreparationBusy(false));
  };
  const hasAnyFilteredResults =
    anchored
      ? (groups?.length ?? 0) > 0
      : filteredBestMatch.length > 0 ||
        filteredActions.length > 0 ||
        filteredGoto.length > 0 ||
        hasCommandsSectionResults ||
        (groups?.length ?? 0) > 0;

  // The flat keyboard-navigable list, in visual order (production sections,
  // then COMMANDS, then SEARCH — matching the section order rendered below).
  const navRows = useMemo<NavRow[]>(() => {
    const rows: NavRow[] = [];
    if (!anchored) {
      filteredBestMatch.forEach((item, i) =>
        rows.push({ key: `best:${item.actionKind}:${i}`, kind: 'action', item }),
      );
      filteredActions.forEach((item, i) =>
        rows.push({ key: `act:${item.actionKind}:${i}`, kind: 'action', item }),
      );
      filteredGoto.forEach((item) => rows.push({ key: `goto:${item.sheetId}`, kind: 'goto', item }));
      filteredCommands.forEach((entry) =>
        rows.push({ key: `cmd:${entry.descriptor.id}`, kind: 'command', entry }),
      );
      filteredLauncherCommands.forEach((item) =>
        rows.push({ key: `launcher:${item.contributionId}`, kind: 'launcher', item }),
      );
      filteredHideTargets.forEach((target) =>
        rows.push({ key: `hide:${target.contributionId}`, kind: 'visibility', action: 'hide', target }),
      );
      filteredRevealTargets.forEach((target) =>
        rows.push({ key: `reveal:${target.contributionId}`, kind: 'visibility', action: 'reveal', target }),
      );
    }
    (groups ?? []).forEach((group) => {
      for (const hit of [group.primary, ...group.supporting]) {
        rows.push({ key: `hit:${hit.part.part_id}:${hit.binding.binding_id}`, kind: 'search', hit });
      }
    });
    return rows;
  }, [
    filteredBestMatch,
    filteredActions,
    filteredGoto,
    filteredCommands,
    filteredLauncherCommands,
    filteredHideTargets,
    filteredRevealTargets,
    groups,
    anchored,
  ]);

  // The highlighted row. Clamp on read (rather than a setState-in-effect) so a
  // shrinking result set never points past the end.
  const [rawActiveIndex, setActiveIndex] = useState(0);
  const activeIndex = navRows.length === 0 ? 0 : Math.min(rawActiveIndex, navRows.length - 1);

  const launchAction = (item: PaletteActionItem) => {
    onLaunchAction(item.actionKind, item.sourceColumn);
    onClose();
  };
  const navigateSheet = (item: PaletteGotoItem) => {
    onNavigateSheet(item.sheetId);
    onClose();
  };
  const navigateHit = (hit: ProjectSearchHitWire) => {
    onNavigateSearchHit(hit);
    onClose();
  };
  // COMMANDS activations mirror their existing click behavior exactly (no
  // auto-close): these are toggles/launchers a user may fire several times in
  // a row (e.g. hide one contribution, reveal another) without leaving the
  // palette, same as clicking them always has.
  const runCommand = (entry: WorkbenchCommandEntry) => {
    void entry.run();
  };
  const runLauncher = (item: PaletteLauncherCommand) => {
    item.run();
  };
  const toggleVisibility = (action: 'hide' | 'reveal', target: WorkbenchVisibilityTarget) => {
    if (action === 'hide') onHideContribution(target);
    else onRevealContribution(target);
  };
  const activateRow = (row: NavRow | undefined) => {
    if (!row) return;
    if (row.kind === 'action') launchAction(row.item);
    else if (row.kind === 'goto') navigateSheet(row.item);
    else if (row.kind === 'command') runCommand(row.entry);
    else if (row.kind === 'launcher') runLauncher(row.item);
    else if (row.kind === 'visibility') toggleVisibility(row.action, row.target);
    else navigateHit(row.hit);
  };

  const onInputKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setActiveIndex((prev) => (navRows.length ? (prev + 1) % navRows.length : 0));
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActiveIndex((prev) => (navRows.length ? (prev - 1 + navRows.length) % navRows.length : 0));
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      activateRow(navRows[activeIndex]);
      return;
    }
    if (event.key === 'Tab') {
      // ⇥ configure: on an action row this opens the same configure-first
      // drawer (never a direct run). On other rows it is a no-op that keeps
      // focus in the palette.
      const row = navRows[activeIndex];
      if (row?.kind === 'action') {
        event.preventDefault();
        launchAction(row.item);
      } else if (row) {
        event.preventDefault();
      }
    }
  };

  const rowIndex = (predicate: (row: NavRow) => boolean) => navRows.findIndex(predicate);

  return createPortal(
    <div
      ref={backdropRef}
      className="workbench-command-palette-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      onKeyDown={(event) => {
        // Esc closes the palette from anywhere inside it (a focused SEARCH
        // control or a result row, not just the query input).
        if (event.key === 'Escape') {
          event.stopPropagation();
          onClose();
        }
      }}
    >
      <section
        className="workbench-command-palette"
        data-testid="workbench-region-commandPalette"
        data-host="commandPalette"
        aria-label="Workbench command palette"
      >
        <div className="workbench-command-palette-search">
          <Search size={15} />
          <input
            ref={inputRef}
            className="workbench-command-palette-input"
            data-testid="command-palette-input"
            aria-label="Command palette"
            placeholder="Search actions, sheets, rows, and commands…"
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
            onKeyDown={onInputKeyDown}
          />
          <button
            type="button"
            className="icon-btn"
            aria-label="Close command palette"
            onClick={onClose}
          >
            <X size={15} />
          </button>
        </div>

        <div className="workbench-command-palette-body">
          {!anchored && (
            <>
              <PaletteProductionSections
                filteredBestMatch={filteredBestMatch}
                filteredActions={filteredActions}
                filteredGoto={filteredGoto}
                activeIndex={activeIndex}
                actionRowIndex={(item) =>
                  rowIndex((row) => row.kind === 'action' && row.item === item)
                }
                gotoRowIndex={(item) => rowIndex((row) => row.kind === 'goto' && row.item === item)}
                onHover={setActiveIndex}
                onLaunchAction={launchAction}
                onNavigateSheet={navigateSheet}
              />

              <PaletteCommandsSection
                commands={filteredCommands}
                launcherCommands={filteredLauncherCommands}
                hideTargets={filteredHideTargets}
                revealTargets={filteredRevealTargets}
                activeIndex={activeIndex}
                commandRowIndex={(entry) =>
                  rowIndex((row) => row.kind === 'command' && row.entry === entry)
                }
                launcherRowIndex={(item) =>
                  rowIndex((row) => row.kind === 'launcher' && row.item === item)
                }
                visibilityRowIndex={(action, target) =>
                  rowIndex(
                    (row) => row.kind === 'visibility' && row.action === action && row.target === target,
                  )
                }
                onHover={setActiveIndex}
                onLaunchCommand={runCommand}
                onLaunchLauncher={runLauncher}
                onHideContribution={onHideContribution}
                onRevealContribution={onRevealContribution}
              />
            </>
          )}

          {!anchored && query.trim() !== '' && !hasAnyFilteredResults && (
            <div className="palette-empty-state" data-testid="palette-empty-state">
              No commands or actions match “{query.trim()}”.
            </div>
          )}

          {/* SEARCH section: controls stay mounted whenever the palette is open
              (so mode/rerank can be set before typing); only the results render
              once there is a query. */}
          <PaletteSearchSection
            mode={mode}
            onModeChange={setMode}
            rerank={rerank}
            onRerankChange={setRerank}
            namedIndexes={namedIndexes}
            namedIndexId={namedIndexId}
            onNamedIndexChange={setNamedIndexId}
            namedIndexError={namedIndexError}
            canWatch={canWatch}
            watchBusy={watchBusy}
            watchStatus={watchStatus}
            onCreateWatch={createSearchWatch}
            canSaveLens={canSaveLens}
            saveBusy={saveBusy}
            saveStatus={saveStatus}
            onSaveLens={saveSearchLens}
            searchError={searchError}
            groups={groups}
            coverage={coverage}
            query={query}
            sheetName={sheetName}
            anchorLabel={anchorLabel}
            anchored={anchored}
            onClearAnchor={clearAnchor}
            activeIndex={activeIndex}
            rowIndexOfHit={(hit) => rowIndex((row) => row.kind === 'search' && row.hit === hit)}
            onHoverHit={setActiveIndex}
            onActivateHit={navigateHit}
            onFindSimilar={findSimilar}
            setupBusy={setupBusy}
            setupError={setupError}
            setupOperation={setupOperation}
            onStartSetup={startSetup}
            onFinishSetup={finishSetup}
            preparationBusy={preparationBusy}
            preparationError={preparationError}
            canPrepareGap={Boolean(projectId)}
            activeSheetId={activeSearchSheetId}
            onPrepareGap={prepareSearchGap}
            onViewJobs={() => {
              onOpenJobs?.();
              onClose();
            }}
          />
          {projectId && preparationBatchId && (
            <SearchPreparationReceipt
              key={preparationBatchId}
              projectId={projectId}
              batchId={preparationBatchId}
              onBatchChange={(batch) => setPreparationBatchId(batch.id)}
              onDone={() => setPreparationBatchId(null)}
              onError={setPreparationError}
              onViewJobs={onOpenJobs == null ? undefined : () => {
                onOpenJobs();
                onClose();
              }}
            />
          )}
        </div>

        <div className="workbench-command-palette-footer">
          <span className="palette-key-legend" data-testid="palette-key-legend">
            <kbd>↑↓</kbd> navigate · <kbd>↵</kbd> run · <kbd>⇥</kbd> configure · <kbd>esc</kbd> close
          </span>
          <span
            className="workbench-command-palette-status"
            data-testid="workbench-command-palette-last-action"
          >
            {lastAction || 'Ready'}
          </span>
        </div>
      </section>
    </div>,
    topLayerPortalRoot(),
  );
}
