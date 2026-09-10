// Shared nav chrome: frisket wordmark (→ picker), the project-name dropdown
// for quick project switching, and the small account link. Route parsing /
// navigation helpers live in src/routes.ts.

import {
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
  type RefObject,
} from 'react';
import { PanelSelect } from './PanelSelect';
import {
  Archive,
  ChevronDown,
  Database,
  Download,
  FileArchive,
  FileSpreadsheet,
  FileText,
  FolderOpen,
  Globe,
  Settings,
  Trash2,
  Users,
} from 'lucide-react';
import {
  ConfirmationRequiredError,
  listProjects,
  updateProject,
  type ActionCatalogEntry,
  type GoogleSheetsExportInput,
  type ProjectInfo,
  type OAuthConnectionInfo,
  type RunEstimateClaim,
  type SheetViewExportOptions,
  type SheetMeta,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type { ProjectApiPort } from '../api/ports';
import { canReviewProject } from '../api/projectRole';
import { useEditionModule } from '../editions/module';
import type { ExportTarget } from '../exportTargets';
import { navigate } from '../routes';
import { useShellIdentity } from '../shellIdentity';
import { useNativePopover } from '../hooks/useNativePopover';
import { useAnchoredPosition, type AnchoredPosition } from '../hooks/useAnchoredPosition';
import { ExportColumnTablesModal } from './ExportColumnTablesModal';
import { sendProductTelemetry } from '../telemetry/productTelemetry';

export type ExportModalKind = 'dataset' | 'google_sheets' | 'column_tables';
type GoogleSheetsSourceKind = 'current_sheet' | 'current_view' | 'all_sheets';
type GoogleSheetsDestinationKind = 'new_spreadsheet' | 'update_existing';

type GoogleSheetsConfirmationChallenge = {
  /** The normalized first submission, retained by value across every retry. */
  input: GoogleSheetsExportInput;
  message: string;
  claims: RunEstimateClaim[];
  promiseSetHash: string;
};

function cloneGoogleSheetsCurrentView(
  options: SheetViewExportOptions | null | undefined,
): SheetViewExportOptions | null {
  if (!options) return null;
  return {
    ...(options.filter
      ? {
          filter: JSON.parse(JSON.stringify(options.filter)) as NonNullable<
            SheetViewExportOptions['filter']
          >,
        }
      : {}),
    ...(options.sort ? { sort: options.sort.map((rule) => ({ ...rule })) } : {}),
  };
}

// Filter-on-dispatchability:
// exportTargetsFromCatalog (web/src/exportTargets.ts) is the ONLY discovery
// rule for what COULD render here — this map is the ONLY dispatch rule for
// what ACTUALLY renders. A target only becomes a row once its destinationKind
// has a modal handler below, so a catalog entry this group can't yet open
// (e.g. a future jsonl/parquet destination) never shows up as a dead click.
// Wiring a new destination is one change here — not a second allowlist that
// can drift from the dispatch switch (the bug that once let a second Act
// export flyout disagree with this canonical project-export surface).
const EXPORT_TARGET_MODAL_KIND: Partial<Record<string, ExportModalKind>> = {
  csv: 'dataset',
  google_sheets: 'google_sheets',
};

type ProjectMenuState = {
  open: boolean;
  projects: ProjectInfo[] | null;
  exportOpen: boolean;
  exportTargets: ExportTarget[] | null;
  exportModal: ExportModalKind | null;
};

type ProjectMenuAction =
  | { type: 'toggleOpen' }
  | { type: 'closeMenu' }
  | { type: 'projectsLoaded'; projects: ProjectInfo[] }
  | { type: 'toggleExportOpen' }
  | { type: 'exportTargetsLoaded'; targets: ExportTarget[] }
  | { type: 'openExportModal'; modal: ExportModalKind }
  | { type: 'closeExportModal' };

const PROJECT_MENU_INITIAL_STATE: ProjectMenuState = {
  open: false,
  projects: null,
  exportOpen: false,
  exportTargets: null,
  exportModal: null,
};

function projectMenuReducer(
  state: ProjectMenuState,
  action: ProjectMenuAction,
): ProjectMenuState {
  switch (action.type) {
    case 'toggleOpen':
      return { ...state, open: !state.open, exportOpen: state.open ? false : state.exportOpen };
    case 'closeMenu':
      return { ...state, open: false, exportOpen: false };
    case 'projectsLoaded':
      return { ...state, projects: action.projects };
    case 'toggleExportOpen':
      return { ...state, exportOpen: !state.exportOpen };
    case 'exportTargetsLoaded':
      return { ...state, exportTargets: action.targets };
    case 'openExportModal':
      return {
        ...state,
        open: false,
        exportOpen: false,
        exportModal: action.modal,
      };
    case 'closeExportModal':
      return { ...state, exportModal: null };
    default:
      return state;
  }
}

function ExportDataGroup({
  currentSheet,
  exportOpen,
  targets,
  canExport,
  onOpenModal,
  onToggle,
}: {
  currentSheet?: SheetMeta | null;
  exportOpen: boolean;
  targets: ExportTarget[] | null;
  // Bulk sheet dataset export is reviewer-tier on the backend
  // -- one rung above the ordinary viewer read of a sheet's grid. Google
  // Sheets export dispatches through a separate, already editor-tier action
  // route and is unaffected by this flag.
  canExport: boolean;
  onOpenModal(modal: ExportModalKind): void;
  onToggle(): void;
}) {
  return (
    <div data-testid="export-data-group">
      <button
        type="button"
        className="menu-item"
        data-testid="export-menu-open"
        onClick={onToggle}
      >
        <Download size={12} />
        <span className="menu-item-name">Export data</span>
        <ChevronDown size={11} />
      </button>
      {exportOpen && (
        <div className="menu-nested" data-testid="export-menu">
          {targets === null ? (
            <div className="menu-empty">Loading…</div>
          ) : (
            (() => {
              // Never render a row this group can't dispatch — see
              // EXPORT_TARGET_MODAL_KIND above. A viewer can't reach the
              // reviewer-tier dataset export routes, so that row is dropped
              // rather than left as a guaranteed 403 click.
              const dispatchable = targets.filter(
                (target) =>
                  EXPORT_TARGET_MODAL_KIND[target.destinationKind] !== undefined &&
                  (target.destinationKind !== 'csv' || canExport),
              );
              if (dispatchable.length === 0) {
                return <div className="menu-empty">No export targets</div>;
              }
              return dispatchable.map((target) => {
                const modalKind = EXPORT_TARGET_MODAL_KIND[target.destinationKind]!;
                return (
                  <button
                    key={target.actionKind}
                    type="button"
                    className="menu-item"
                    data-testid={`export-target-${target.destinationKind}`}
                    disabled={target.destinationKind === 'csv' && !currentSheet}
                    onClick={() => onOpenModal(modalKind)}
                  >
                    {target.destinationKind === 'google_sheets' ? (
                      <FileSpreadsheet size={12} />
                    ) : (
                      <Download size={12} />
                    )}
                    <span className="menu-item-name">
                      {target.destinationKind === 'google_sheets'
                        ? 'Google Sheets'
                        : target.destinationKind === 'csv'
                          ? 'CSV or Excel'
                          : target.label}
                    </span>
                  </button>
                );
              });
            })()
          )}
        </div>
      )}
    </div>
  );
}

function WorkLogExportGroup({
  projectApi,
  canExport,
  onCloseMenu,
}: {
  projectApi: ProjectApiPort;
  // The work log carries prompt text and before/after example cell values --
  // reviewer-tier on the backend (export_work_log*), one rung above a
  // viewer's ordinary reads. A viewer never sees this group: no dead click.
  canExport: boolean;
  onCloseMenu(): void;
}) {
  const { projectId } = useWorkspaceStores().chromePreferences;
  const requested = () => {
    sendProductTelemetry(
      { type: 'Export.requested', properties: { exportKind: 'work_log' } },
      projectId,
    );
    onCloseMenu();
  };
  if (!canExport) return null;
  return (
    <div data-testid="export-work-log-group">
      <a
        className="menu-item"
        data-testid="export-work-log"
        href={projectApi.workLogExportUrl('md')}
        download
        onClick={requested}
      >
        <FileArchive size={12} />
        <span className="menu-item-name">Work log (Markdown)</span>
      </a>
      <a
        className="menu-item"
        data-testid="export-work-log-html"
        href={projectApi.workLogExportUrl('html')}
        download
        onClick={requested}
      >
        <Globe size={12} />
        <span className="menu-item-name">Work log (HTML)</span>
      </a>
      <a
        className="menu-item"
        data-testid="export-work-log-pdf"
        href={projectApi.workLogExportUrl('pdf')}
        download
        onClick={requested}
      >
        <FileText size={12} />
        <span className="menu-item-name">Work log (PDF)</span>
      </a>
    </div>
  );
}

/** This group is UNRELATED to the "Archive project" flag button above (testid
 *  project-archive): it's a pair of navigate() shortcuts into settings
 *  surfaces that already exist (Data Management's Compact action, General's
 *  type-to-confirm danger zone), placed AFTER both export groups so the two
 *  rare/high-stakes actions (a snapshot, a permanent delete) sink to the
 *  bottom of the menu rather than sitting beside routine per-project
 *  toggles. The testid "project-archive-group" is pinned by
 *  test_export_menu_organization and kept verbatim.
 *
 *  The "Data & snapshots" heading disambiguates this group from the
 *  "Archive project" button beside it; the button's own label stays
 *  "Archive project" — project-archive-hide.spec.ts pins 'Archive'/'Unarchive'
 *  text for HomeScreen's card-menu equivalent, so keeping this label's
 *  semantics aligned with that naming is the smaller, honest fix. */
function ProjectArchiveGroup({
  projectId,
  onCloseMenu,
}: {
  projectId: string;
  onCloseMenu(): void;
}) {
  return (
    <>
      <div className="menu-section-label">Data &amp; snapshots</div>
      <div data-testid="project-archive-group">
        <button
          type="button"
          className="menu-item"
          data-testid="database-snapshot"
          onClick={() => {
            onCloseMenu();
            navigate({
              kind: 'settings',
              projectId,
              scope: 'project',
              section: 'data-management',
            });
          }}
        >
          <Database size={12} />
          <span className="menu-item-name">Database snapshot</span>
        </button>
        <button
          type="button"
          className="menu-item"
          data-testid="delete-project"
          onClick={() => {
            onCloseMenu();
            navigate({
              kind: 'settings',
              projectId,
              scope: 'project',
              section: 'general',
            });
          }}
        >
          <Trash2 size={12} />
          <span className="menu-item-name">Delete project</span>
        </button>
      </div>
    </>
  );
}

function ProjectList({
  project,
  projects,
  onSelectProject,
}: {
  project: ProjectInfo;
  projects: ProjectInfo[] | null;
  onSelectProject(project: ProjectInfo): void;
}) {
  if (projects === null) return <div className="menu-empty">Loading…</div>;
  // Archived projects don't clutter the quick-switch list; they live under the
  // Home screen's Archive scope. The current project always stays visible.
  const items: ReactNode[] = [];
  for (const candidate of projects) {
    if (candidate.id !== project.id && candidate.archived) continue;
    items.push(
      <button
        key={candidate.id}
        type="button"
        className={`menu-item${candidate.id === project.id ? ' current' : ''}`}
        data-testid={`menu-project-${candidate.id}`}
        onClick={() => onSelectProject(candidate)}
      >
        <FolderOpen size={12} />
        <span className="menu-item-name">{candidate.name}</span>
      </button>,
    );
  }
  return items;
}

function ProjectMenuPopover({
  popoverRef,
  menuPos,
  currentSheet,
  exportOpen,
  exportTargets,
  project,
  projectApi,
  projects,
  onCloseMenu,
  onOpenExportModal,
  onSelectProject,
  onToggleExportOpen,
}: {
  popoverRef: RefObject<HTMLDivElement>;
  menuPos: AnchoredPosition | null;
  currentSheet?: SheetMeta | null;
  exportOpen: boolean;
  exportTargets: ExportTarget[] | null;
  project: ProjectInfo;
  projectApi: ProjectApiPort;
  projects: ProjectInfo[] | null;
  onCloseMenu(): void;
  onOpenExportModal(modal: ExportModalKind): void;
  onSelectProject(project: ProjectInfo): void;
  onToggleExportOpen(): void;
}) {
  const { identityMode, me } = useShellIdentity();
  const { settingsSections } = useEditionModule();
  // Bulk data-takeout routes (project/CSV/work-log export) are reviewer-tier
  // on the backend, one rung above a viewer's ordinary reads.
  const canExport = canReviewProject(project);
  // Sharing only exists as a settings section on the team-and-above edition
  // (CAPABILITY_GATED_OPEN_SECTIONS in openSettingsRegistry.ts). Deriving
  // availability from the same edition-composed section list the settings
  // route itself resolves against — rather than re-deriving "team edition"
  // locally — means this item can never drift from what SettingsWorkspace
  // would actually render if it opened the link: no dead click on the local,
  // single-user edition.
  const canShareProject = useMemo(
    () =>
      settingsSections.some(
        (section) => section.scope === 'project' && section.section === 'access',
      ),
    [settingsSections],
  );
  // Ownership line, TIER-HONEST: the local tier has no teams/collaborators, so
  // it reads "Local workspace" — never a fabricated team or org.
  const ownershipLine = identityMode
    ? me?.email
      ? `${me.email} · you own`
      : 'you own'
    : 'Local workspace';
  return (
    <div
      ref={popoverRef}
      className="menu-pop project-menu-pop"
      data-testid="project-menu"
      // Reset the UA popover centering so the JS-anchored top-layer placement
      // below applies instead.
      style={
        menuPos
          ? { position: 'fixed', inset: 'auto', top: menuPos.top, bottom: menuPos.bottom, left: menuPos.left, right: 'auto', width: menuPos.width, margin: 0 }
          : { position: 'fixed', visibility: 'hidden' }
      }
    >
      <div className="project-menu-header" data-testid="project-menu-header">
        <span className="project-menu-tile">
          <FolderOpen size={14} />
        </span>
        <span className="project-menu-identity">
          <span className="project-menu-name">{project.name}</span>
          <span className="project-menu-ownership">{ownershipLine}</span>
        </span>
      </div>
      <div className="menu-sep" />
      <div className="menu-section-label">This project</div>
      <button
        type="button"
        className="menu-item"
        data-testid="project-settings-open"
        onClick={() => {
          onCloseMenu();
          navigate({
            kind: 'settings',
            projectId: project.id,
            scope: 'project',
            section: 'general',
          });
        }}
      >
        <Settings size={12} className="menu-item-icon" />
        <span className="menu-item-name">Project settings</span>
      </button>
      <button
        type="button"
        className="menu-item"
        data-testid="project-sources-open"
        onClick={() => {
          onCloseMenu();
          // The workspace owns the focused Sources & connections manager.
          window.dispatchEvent(new Event('frisket:open-sources'));
        }}
      >
        <Database size={12} className="menu-item-icon" />
        <span className="menu-item-name">Sources &amp; connections</span>
      </button>
      {canShareProject && (
        <button
          type="button"
          className="menu-item"
          data-testid="project-sharing-open"
          onClick={() => {
            onCloseMenu();
            navigate({
              kind: 'settings',
              projectId: project.id,
              scope: 'project',
              section: 'access',
            });
          }}
        >
          <Users size={12} className="menu-item-icon" />
          <span className="menu-item-name">Sharing…</span>
        </button>
      )}
      <button
        type="button"
        className="menu-item"
        data-testid="project-archive"
        onClick={() => {
          onCloseMenu();
          // Archive is a FLAG (never a deletion): mark it, then return Home
          // where the Archive scope surfaces it.
          void updateProject(project.id, { archived: true }).then(() =>
            navigate({ kind: 'picker' }),
          );
        }}
      >
        <Archive size={12} className="menu-item-icon" />
        <span className="menu-item-name">Archive project</span>
      </button>
      <div className="menu-sep" />
      <ExportDataGroup
        currentSheet={currentSheet}
        exportOpen={exportOpen}
        targets={exportTargets}
        canExport={canExport}
        onOpenModal={onOpenExportModal}
        onToggle={onToggleExportOpen}
      />
      {canExport && (
        <>
          <div className="menu-sep" />
          <WorkLogExportGroup
            projectApi={projectApi}
            canExport={canExport}
            onCloseMenu={onCloseMenu}
          />
        </>
      )}
      <div className="menu-sep" />
      <ProjectArchiveGroup projectId={project.id} onCloseMenu={onCloseMenu} />
      <div className="menu-sep" />
      <div className="menu-section-label">Switch project</div>
      <ProjectList project={project} projects={projects} onSelectProject={onSelectProject} />
      <button
        type="button"
        className="menu-item menu-all-projects"
        data-testid="menu-all-projects"
        onClick={() => navigate({ kind: 'picker' })}
      >
        <FolderOpen size={12} className="menu-item-icon" />
        <span className="menu-item-name">All projects — Home</span>
      </button>
    </div>
  );
}

/** Project-level export modal host. CSV and Google Sheets are shared by the
 *  project ▾ menu and Act; column tables is launched from Act Export. */
export function ProjectExportModals({
  modal,
  project,
  projectApi,
  currentSheet,
  sheets,
  currentSheetExportOptions,
  columnTablesExportEntry,
  onClose,
}: {
  modal: ExportModalKind | null;
  project: ProjectInfo;
  projectApi: ProjectApiPort;
  currentSheet?: SheetMeta | null;
  sheets: SheetMeta[];
  currentSheetExportOptions?: SheetViewExportOptions | null;
  columnTablesExportEntry?: ActionCatalogEntry | null;
  onClose(): void;
}) {
  const hasCurrentViewExport = Boolean(
    currentSheetExportOptions?.filter || currentSheetExportOptions?.sort,
  );
  return (
    <>
      {modal === 'dataset' && currentSheet && (
        <ExportDatasetModal
          sheets={sheets}
          currentSheet={currentSheet}
          currentSheetExportOptions={currentSheetExportOptions}
          hasCurrentViewExport={hasCurrentViewExport}
          projectApi={projectApi}
          onClose={onClose}
        />
      )}
      {modal === 'google_sheets' && (
        <ExportGoogleSheetsModal
          currentSheet={currentSheet}
          currentSheetExportOptions={currentSheetExportOptions}
          hasCurrentViewExport={hasCurrentViewExport}
          projectName={project.name}
          projectId={project.id}
          onClose={onClose}
        />
      )}
      {modal === 'column_tables' && currentSheet && (
        <ExportColumnTablesModal
          projectId={project.id}
          currentSheet={currentSheet}
          catalogEntry={columnTablesExportEntry}
          onClose={onClose}
        />
      )}
    </>
  );
}

/** Project-name dropdown: quick-switch between projects + "All projects". */
export function ProjectMenu({
  project,
  projectApi,
  currentSheet,
  sheets,
  currentSheetExportOptions,
  catalogExportTargets,
}: {
  project: ProjectInfo;
  projectApi: ProjectApiPort;
  currentSheet?: SheetMeta | null;
  sheets: SheetMeta[];
  currentSheetExportOptions?: SheetViewExportOptions | null;
  catalogExportTargets: ExportTarget[];
}) {
  const [state, dispatch] = useReducer(projectMenuReducer, PROJECT_MENU_INITIAL_STATE);
  const {
    open,
    projects,
    exportOpen,
    exportTargets,
    exportModal,
  } = state;
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    listProjects()
      .then((loaded) => dispatch({ type: 'projectsLoaded', projects: loaded }))
      .catch(() => dispatch({ type: 'projectsLoaded', projects: [] }));
    dispatch({ type: 'exportTargetsLoaded', targets: catalogExportTargets });
  }, [catalogExportTargets, open]);

  // Top-layer popover: plain ref+toggle
  // default dismissal; the trigger testid is the ignoreSelector so its own
  // click keeps sole ownership of the open/close toggle.
  useNativePopover(menuRef, () => dispatch({ type: 'closeMenu' }), {
    enabled: open,
    ignoreSelector: '[data-testid="switch-project"]',
  });
  // Fixed-position anchor now that the menu is a top-layer element — it no
  // longer inherits placement from `.project-menu`'s CSS positioned ancestor
  // (styles.css `.project-menu-pop`, left-aligned under the trigger — a
  // deliberate override of the shared `.menu-pop` right:0, "a wide menu
  // would push off-screen" per the adjacent comment).
  const menuPos = useAnchoredPosition(triggerRef, {
    enabled: open,
    align: 'left',
    width: 264,
    gap: 4,
  });

  return (
    <div className="project-menu" ref={rootRef}>
      <button
        type="button"
        ref={triggerRef}
        className="brand-project brand-project-btn"
        data-testid="switch-project"
        title="Switch project"
        onClick={() => dispatch({ type: 'toggleOpen' })}
      >
        {project.name} <ChevronDown size={11} />
      </button>
      {open && (
        <ProjectMenuPopover
          popoverRef={menuRef}
          menuPos={menuPos}
          currentSheet={currentSheet}
          exportOpen={exportOpen}
          exportTargets={exportTargets}
          project={project}
          projectApi={projectApi}
          projects={projects}
          onCloseMenu={() => dispatch({ type: 'closeMenu' })}
          onOpenExportModal={(modal) => dispatch({ type: 'openExportModal', modal })}
          onSelectProject={(selectedProject) => {
            dispatch({ type: 'closeMenu' });
            if (selectedProject.id !== project.id) {
              navigate({ kind: 'project', projectId: selectedProject.id });
            }
          }}
          onToggleExportOpen={() => dispatch({ type: 'toggleExportOpen' })}
        />
      )}
      <ProjectExportModals
        modal={exportModal}
        project={project}
        projectApi={projectApi}
        currentSheet={currentSheet}
        sheets={sheets}
        currentSheetExportOptions={currentSheetExportOptions}
        onClose={() => dispatch({ type: 'closeExportModal' })}
      />
    </div>
  );
}

function ExportDatasetModal({
  sheets,
  currentSheet,
  currentSheetExportOptions,
  hasCurrentViewExport,
  projectApi,
  onClose,
}: {
  sheets: SheetMeta[];
  currentSheet: SheetMeta;
  currentSheetExportOptions?: SheetViewExportOptions | null;
  hasCurrentViewExport: boolean;
  projectApi: ProjectApiPort;
  onClose: () => void;
}) {
  const { projectId } = useWorkspaceStores().chromePreferences;
  const [format, setFormat] = useState<'csv' | 'xlsx'>('csv');
  const [scope, setScope] = useState<'entire' | 'current_view'>('entire');
  const [selectedSheetIds, setSelectedSheetIds] = useState<string[]>([currentSheet.id]);
  const currentSheetOnly =
    selectedSheetIds.length === 1 && selectedSheetIds[0] === currentSheet.id;
  const currentViewAvailable = hasCurrentViewExport && currentSheetOnly;
  const effectiveScope = currentViewAvailable ? scope : 'entire';
  const exportUrl = selectedSheetIds.length > 0
    ? projectApi.sheetDatasetExportUrl({
        format,
        sheetIds: selectedSheetIds,
        currentView: effectiveScope === 'current_view' ? currentSheetExportOptions : null,
      })
    : null;
  const downloadLabel =
    format === 'xlsx'
      ? `Export ${selectedSheetIds.length} sheet${selectedSheetIds.length === 1 ? '' : 's'} to Excel`
      : selectedSheetIds.length > 1
        ? `Export ${selectedSheetIds.length} CSVs as ZIP`
        : 'Export CSV';

  const toggleSheet = (sheetId: string) => {
    setSelectedSheetIds((selected) =>
      selected.includes(sheetId)
        ? selected.filter((candidate) => candidate !== sheetId)
        : [...selected, sheetId],
    );
  };

  return (
    <div className="modal-backdrop" data-testid="export-data-modal">
      <div className="modal-card export-dataset-modal">
        <div className="modal-title">
          <Download size={15} /> Export data
        </div>
        <p className="modal-body-text">Choose a format, scope, and one or more sheets.</p>

        <fieldset className="export-dataset-section">
          <legend>Format</legend>
          <div className="export-format-grid">
            <label className={`export-choice-card${format === 'csv' ? ' selected' : ''}`}>
              <input
                type="radio"
                name="export-format"
                value="csv"
                checked={format === 'csv'}
                data-testid="export-format-csv"
                onChange={() => setFormat('csv')}
              />
              <span><strong>CSV</strong><small>One file, or a ZIP for multiple sheets</small></span>
            </label>
            <label className={`export-choice-card${format === 'xlsx' ? ' selected' : ''}`}>
              <input
                type="radio"
                name="export-format"
                value="xlsx"
                checked={format === 'xlsx'}
                data-testid="export-format-xlsx"
                onChange={() => setFormat('xlsx')}
              />
              <span><strong>Excel</strong><small>One workbook with a tab per sheet</small></span>
            </label>
          </div>
        </fieldset>

        <fieldset className="export-dataset-section">
          <legend>Rows</legend>
          <label className="export-scope-option">
            <input
              type="radio"
              name="export-scope"
              checked={effectiveScope === 'entire'}
              data-testid="export-scope-entire"
              onChange={() => setScope('entire')}
            />
            Entire selected sheets
          </label>
          <label className="export-scope-option">
            <input
              type="radio"
              name="export-scope"
              checked={effectiveScope === 'current_view'}
              disabled={!currentViewAvailable}
              data-testid="export-scope-current-view"
              onChange={() => setScope('current_view')}
            />
            Current filtered/sorted view
            {!hasCurrentViewExport && <small>No filter or sort is active</small>}
            {hasCurrentViewExport && !currentSheetOnly && <small>Select only the current sheet</small>}
          </label>
        </fieldset>

        <fieldset className="export-dataset-section">
          <legend className="export-sheet-heading">
            <span>Sheets</span>
            <button
              type="button"
              className="export-select-all"
              data-testid="export-select-all-sheets"
              onClick={() => setSelectedSheetIds(sheets.map((sheet) => sheet.id))}
            >
              Select all
            </button>
          </legend>
          <div className="export-sheet-list">
            {sheets.map((sheet) => (
              <label key={sheet.id} className="export-sheet-option">
                <input
                  type="checkbox"
                  checked={selectedSheetIds.includes(sheet.id)}
                  data-testid={`export-sheet-${sheet.id}`}
                  onChange={() => toggleSheet(sheet.id)}
                />
                <span>{sheet.name}</span>
                <small>{sheet.rowCount.toLocaleString()} rows</small>
              </label>
            ))}
          </div>
        </fieldset>

        <div className="export-dataset-summary" aria-live="polite">
          {selectedSheetIds.length === 0
            ? 'Select at least one sheet.'
            : format === 'xlsx'
              ? 'Selected sheets will become tabs in one workbook.'
              : selectedSheetIds.length > 1
                ? 'Selected sheets will download as BOM-prefixed CSV files in one ZIP.'
                : 'The CSV includes an Excel-friendly UTF-8 BOM.'}
        </div>
        <div className="form-actions">
          <button
            type="button"
            className="btn"
            data-testid="export-csv-cancel"
            onClick={onClose}
          >
            Cancel
          </button>
          {exportUrl ? (
            <a
              className="btn btn-primary"
              data-testid="export-dataset-download"
              href={exportUrl}
              download
              onClick={() => {
                sendProductTelemetry({
                  type: 'Export.requested',
                  properties: { exportKind: format === 'xlsx' ? 'sheet_xlsx' : 'sheet_csv' },
                }, projectId);
                onClose();
              }}
            >
              {downloadLabel}
            </a>
          ) : (
            <button type="button" className="btn btn-primary" disabled>
              Export
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function ExportGoogleSheetsModal({
  currentSheet,
  currentSheetExportOptions,
  hasCurrentViewExport,
  projectName,
  projectId,
  onClose,
}: {
  currentSheet?: SheetMeta | null;
  currentSheetExportOptions?: SheetViewExportOptions | null;
  hasCurrentViewExport: boolean;
  projectName: string;
  projectId: string;
  onClose: () => void;
}) {
  const { projectApi } = useWorkspaceStores();
  interface ExportGoogleSheetsState {
    connections: OAuthConnectionInfo[] | null;
    // Edition/composition gate (server/action_catalog_hints.py ui_hints.
    // unavailable_reason on the export.google_sheets catalog entry): the
    // local single-user tier has no OAuth flow to connect a Google account
    // at all, so /api/org/oauth/connections and /google/start 404 there.
    // Undefined = not checked yet; null = available; string = the one-line
    // reason to show INSTEAD of the (guaranteed-broken) "Connect Google"
    // affordance below.
    unavailableReason: string | null | undefined;
    connectionId: string;
    sourceKind: GoogleSheetsSourceKind;
    destinationKind: GoogleSheetsDestinationKind;
    spreadsheetTitle: string;
    spreadsheetId: string;
    exporting: boolean;
    error: string | null;
    resultUrl: string | null;
    confirmation: GoogleSheetsConfirmationChallenge | null;
    confirmationText: string;
  }
  type ExportGoogleSheetsPatch =
    | Partial<ExportGoogleSheetsState>
    | ((state: ExportGoogleSheetsState) => Partial<ExportGoogleSheetsState>);
  const [modalState, setModalState] = useReducer(
    (state: ExportGoogleSheetsState, patch: ExportGoogleSheetsPatch): ExportGoogleSheetsState => ({
      ...state,
      ...(typeof patch === 'function' ? patch(state) : patch),
    }),
    {
      connections: null,
      unavailableReason: undefined,
      connectionId: '',
      sourceKind: currentSheet ? 'current_sheet' : 'all_sheets',
      destinationKind: 'new_spreadsheet',
      spreadsheetTitle: `${projectName} export`,
      spreadsheetId: '',
      exporting: false,
      error: null,
      resultUrl: null,
      confirmation: null,
      confirmationText: '',
    },
  );
  const {
    connections,
    unavailableReason,
    connectionId,
    sourceKind,
    destinationKind,
    spreadsheetTitle,
    spreadsheetId,
    exporting,
    error,
    resultUrl,
    confirmation,
    confirmationText,
  } = modalState;

  // Edition/composition gate: read-only, best-effort. On any failure to load
  // the catalog itself, unavailableReason simply stays undefined/null and
  // the modal degrades to its pre-existing behavior (the connections fetch
  // below is the authoritative path either way).
  useEffect(() => {
    let alive = true;
    projectApi
      .listActionCatalog(projectId)
      .then((catalog) => {
        if (!alive) return;
        const entry = catalog.actions.find((action) => action.kind === 'export.google_sheets');
        const reason = entry?.ui_hints?.unavailable_reason;
        setModalState({ unavailableReason: typeof reason === 'string' && reason ? reason : null });
      })
      .catch(() => {
        if (alive) setModalState({ unavailableReason: null });
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  useEffect(() => {
    let alive = true;
    projectApi
      .listOAuthConnections('google')
      .then((loaded) => {
        if (!alive) return;
        setModalState((current) => ({
          connections: loaded,
          connectionId: current.connectionId || loaded[0]?.connection_id || loaded[0]?.id || '',
        }));
      })
      .catch((e) => {
        if (!alive) return;
        setModalState({
          connections: [],
          error: e instanceof Error ? e.message : 'Could not load Google connections',
        });
      });
    return () => {
      alive = false;
    };
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (exporting || (confirmation && confirmationText !== 'confirm')) return;
    setModalState({ exporting: true, error: null, resultUrl: null });
    const frozenInput = confirmation?.input ?? {
      connectionId: connectionId.trim(),
      sourceKind,
      sheetId: currentSheet?.id ?? null,
      currentView: cloneGoogleSheetsCurrentView(currentSheetExportOptions),
      destinationKind,
      spreadsheetTitle: spreadsheetTitle.trim() || 'Frisket export',
      spreadsheetId: spreadsheetId.trim(),
    };
    try {
      const result = await projectApi.exportGoogleSheets(
        confirmation
          ? {
              ...frozenInput,
              confirmation: confirmation.promiseSetHash,
            }
          : frozenInput,
      );
      setModalState({
        resultUrl: result.spreadsheetUrl ?? null,
        confirmation: null,
        confirmationText: '',
      });
    } catch (e) {
      if (e instanceof ConfirmationRequiredError && e.estimate.promise_set_hash) {
        setModalState({
          confirmation: {
            input: frozenInput,
            message: e.message,
            claims: e.estimate.claims ?? [],
            promiseSetHash: e.estimate.promise_set_hash,
          },
          confirmationText: '',
          error: null,
        });
      } else {
        setModalState({
          error: e instanceof Error ? e.message : 'Google Sheets export failed',
          confirmation: null,
          confirmationText: '',
        });
      }
    } finally {
      setModalState({ exporting: false });
    }
  };

  const canExport =
    !unavailableReason &&
    Boolean(connectionId) &&
    (sourceKind === 'all_sheets' || Boolean(currentSheet)) &&
    (destinationKind === 'new_spreadsheet' || spreadsheetId.trim().length > 0);
  const formFrozen = exporting || confirmation !== null;
  const canSubmit = confirmation
    ? confirmationText === 'confirm'
    : canExport;

  return (
    <div className="modal-backdrop" data-testid="export-google-sheets-modal">
      <form className="modal-card" onSubmit={submit}>
        <div className="modal-title">
          <FileSpreadsheet size={15} /> Export to Google Sheets
        </div>
        {unavailableReason ? (
          <div className="form-error" data-testid="export-google-sheets-unavailable" role="alert">
            {unavailableReason}
          </div>
        ) : connections === null ? (
          <div className="menu-empty">Loading…</div>
        ) : connections.length === 0 ? (
          <div className="export-option-stack">
            <a className="btn export-option-btn" href={projectApi.googleOAuthStartUrl()}>
              Connect Google
            </a>
          </div>
        ) : (
          <label className="form-field">
            <span>Google account</span>
            <PanelSelect
              className="form-input"
              value={connectionId}
              disabled={formFrozen}
              onChange={(event) => setModalState({ connectionId: event.target.value })}
            >
              {connections.map((connection) => {
                const id = connection.connection_id || connection.id;
                return (
                  <option key={id} value={id}>
                    {connection.external_email || id}
                  </option>
                );
              })}
            </PanelSelect>
          </label>
        )}
        {!unavailableReason && (
          <>
            <div className="export-option-stack">
              <button
                type="button"
                className={`btn export-option-btn${sourceKind === 'current_sheet' ? ' current' : ''}`}
                disabled={formFrozen || !currentSheet}
                onClick={() => setModalState({ sourceKind: 'current_sheet' })}
              >
                Current sheet
              </button>
              <button
                type="button"
                className={`btn export-option-btn${sourceKind === 'current_view' ? ' current' : ''}`}
                disabled={formFrozen || !hasCurrentViewExport}
                onClick={() => setModalState({ sourceKind: 'current_view' })}
              >
                Current view
              </button>
              <button
                type="button"
                className={`btn export-option-btn${sourceKind === 'all_sheets' ? ' current' : ''}`}
                disabled={formFrozen}
                onClick={() => setModalState({ sourceKind: 'all_sheets' })}
              >
                All sheets
              </button>
              <div className="menu-sep" />
              <button
                type="button"
                className={`btn export-option-btn${destinationKind === 'new_spreadsheet' ? ' current' : ''}`}
                disabled={formFrozen}
                onClick={() => setModalState({ destinationKind: 'new_spreadsheet' })}
              >
                New spreadsheet
              </button>
              <button
                type="button"
                className={`btn export-option-btn${destinationKind === 'update_existing' ? ' current' : ''}`}
                disabled={formFrozen}
                onClick={() => setModalState({ destinationKind: 'update_existing' })}
              >
                Update existing spreadsheet
              </button>
            </div>
            {destinationKind === 'new_spreadsheet' ? (
              <label className="form-field">
                <span>Spreadsheet title</span>
                <input
                  className="form-input"
                  value={spreadsheetTitle}
                  disabled={formFrozen}
                  onChange={(event) => setModalState({ spreadsheetTitle: event.target.value })}
                />
              </label>
            ) : (
              <label className="form-field">
                <span>Spreadsheet ID</span>
                <input
                  className="form-input"
                  value={spreadsheetId}
                  disabled={formFrozen}
                  onChange={(event) => setModalState({ spreadsheetId: event.target.value })}
                  placeholder="1abc..."
                />
              </label>
            )}
          </>
        )}
        {confirmation && (
          <div data-testid="export-google-sheets-confirmation">
            <div className="form-help" data-testid="export-google-sheets-confirmation-message">
              {confirmation.message}
            </div>
            {confirmation.claims.length > 0 && (
              <ul data-testid="export-google-sheets-confirmation-claims">
                {confirmation.claims.map((claim, index) => (
                  <li key={`${claim.field}-${index}`}>{claim.display}</li>
                ))}
              </ul>
            )}
            <label className="form-field">
              <span>Type <strong>confirm</strong> to continue</span>
              <input
                className="form-input"
                data-testid="export-google-sheets-confirmation-input"
                value={confirmationText}
                disabled={exporting}
                autoComplete="off"
                onChange={(event) => setModalState({ confirmationText: event.target.value })}
              />
            </label>
          </div>
        )}
        {error && <div className="form-error">{error}</div>}
        {resultUrl && (
          <a className="form-help" href={resultUrl} target="_blank" rel="noreferrer">
            Open spreadsheet
          </a>
        )}
        <div className="form-actions">
          <button
            type="button"
            className="btn"
            data-testid="export-google-sheets-cancel"
            onClick={onClose}
          >
            Cancel
          </button>
          {!unavailableReason && (
            <button
              type="submit"
              className="btn btn-primary"
              data-testid="export-google-sheets-submit"
              disabled={!canSubmit || exporting || connections?.length === 0}
            >
              {exporting ? 'Exporting...' : confirmation ? 'Confirm export' : 'Export'}
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
