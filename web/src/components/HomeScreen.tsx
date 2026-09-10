// Home / projects screen — the default landing surface and route-home. A
// full-window route screen (not a modal): a top bar (app mark + ⌘K-openable
// command pill + avatar/account menu), a left nav that scopes the list
// (Home · Recent · Starred · Archive; Workspace settings pinned bottom), and a
// responsive card grid of real projects with cover art, a ⋯ overflow menu
// (Archive / Delete / Star), and a live "N sheets · N rows · opened …" meta
// line. Clicking a card opens the project into the workbench.
//
// TIER HONESTY RULE: the local tier has NO teams / "Shared with me" / collaborator
// avatar stacks — those render only on the auth tier. The Home screen never
// fabricates them.

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type RefObject,
} from 'react';
import {
  Archive,
  ArchiveRestore,
  Clock,
  FolderPlus,
  Home,
  MoreHorizontal,
  Pencil,
  Plus,
  Search,
  Settings,
  FlaskConical,
  Star,
  Trash2,
} from 'lucide-react';
import {
  createProject,
  deleteProject,
  listProjects,
  seedSampleProject,
  updateProject,
  type ProjectInfo,
  type MeInfo,
} from '../api/open';
import { remediateServerUnreachable } from '../errors/remediation';
import { formatRelativeTime } from '../format';
import { navigate } from '../routes';
import { useShellIdentity } from '../shellIdentity';
import { useNativePopover } from '../hooks/useNativePopover';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';
import { MenuPop } from './MenuPop';
import { ChromeBarShell } from '../workbench/ChromeBar';
import { AccountMenu } from './AccountMenu';
import { SupportContactNote } from './SupportContactNote';
import { useInstanceIdentity } from '../instanceIdentity';
import { SegmentedToggle } from './PanelPrimitives';
import { fetchProjectSheetCounts } from '../api/homeSheetCounts';
import {
  durationBucket,
  failureCategory,
  sendProductTelemetry,
} from '../telemetry/productTelemetry';

export interface HomeScreenProps {
  onOpen(project: ProjectInfo): void;
}

type NavScope = 'home' | 'recent' | 'starred' | 'archive';
type HomeView = 'grid' | 'list';

const SAMPLE_PROJECT_NAME = 'Sample project';

// Cover art keyed off a project-type hash: a deterministic hue + a faded glyph,
// so a project's card is recognizable at a glance without any stored art.
const COVER_GLYPHS = ['◈', '⛃', '§', '◉', '¶', '▦', '◔', '⧉'];

function hashString(value: string): number {
  let h = 0;
  for (let i = 0; i < value.length; i += 1) {
    h = (h << 5) - h + value.charCodeAt(i);
    h |= 0;
  }
  return Math.abs(h);
}

function coverStyle(project: ProjectInfo): CSSProperties {
  const hue = hashString(project.id) % 360;
  const hue2 = (hue + 40) % 360;
  return {
    background: `linear-gradient(135deg, hsl(${hue} 42% 88%), hsl(${hue2} 46% 80%))`,
  };
}

function coverGlyph(project: ProjectInfo): string {
  return COVER_GLYPHS[hashString(project.id) % COVER_GLYPHS.length];
}

/** Per-project sheet/row counts for a home card. Hoisted to module scope so the
 * network call lives outside the effect (react-doctor no-fetch-in-effect); a
 * non-ok response degrades to zero counts, never a throw. */
function welcomeSeenKey(email: string): string {
  return `frisket-welcome-seen:${email}`;
}

interface CardCounts {
  sheets: number;
  rows: number;
}

interface HomeProjectsController {
  projects: ProjectInfo[] | null;
  counts: Record<string, CardCounts>;
  welcome: { message: string; key: string } | null;
  error: string | null;
  remediation: string | null;
  busy: boolean;
  sampleBusy: boolean;
  reload(): void;
  setError(message: string | null): void;
  dismissWelcome(): void;
  createAndOpen(name: string): void;
  trySampleAndOpen(): void;
  setFlag(project: ProjectInfo, patch: { starred?: boolean; archived?: boolean }): void;
  renameProject(project: ProjectInfo, nextName: string): void;
  removeProject(project: ProjectInfo): void;
}

// Server-data controller for the Home screen: the projects list plus its lazy
// per-project counts, the one-time welcome banner, and every project mutation
// (create / sample-seed / star-archive flag / rename / delete). Extracted from
// HomeScreen so that component owns only view/filter/inline-edit UI state.
function useHomeProjects(
  onOpen: (project: ProjectInfo) => void,
  me: MeInfo | null,
): HomeProjectsController {
  const [projects, setProjects] = useState<ProjectInfo[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [sampleBusy, setSampleBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [remediation, setRemediation] = useState<string | null>(null);
  const [counts, setCounts] = useState<Record<string, CardCounts>>({});
  const [dismissedWelcomeKey, setDismissedWelcomeKey] = useState<string | null>(null);
  const countsRequested = useRef<Set<string> | null>(null);
  countsRequested.current ??= new Set();

  const reload = useCallback(() => {
    listProjects()
      .then((loaded) => {
        setProjects(loaded);
        setError(null);
        setRemediation(null);
      })
      .catch((e: Error) => {
        const remediated = remediateServerUnreachable(e);
        setError(remediated.message);
        setRemediation(remediated.remediation ?? null);
        setProjects([]);
      });
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const welcomeMessage = me?.instance?.welcome_message;
  const welcomeKey = me ? welcomeSeenKey(me.email) : null;
  const welcome = welcomeMessage
    && welcomeKey
    && dismissedWelcomeKey !== welcomeKey
    && !window.localStorage.getItem(welcomeKey)
    ? { message: welcomeMessage, key: welcomeKey }
    : null;

  // Lazily fetch real per-project sheet/row counts for the meta line. Local
  // workspaces hold a handful of projects, so one cheap /sheets call per card
  // (real data, never fabricated) is fine; results are memoized per id.
  useEffect(() => {
    if (!projects) return;
    for (const p of projects) {
      if (countsRequested.current!.has(p.id)) continue;
      countsRequested.current!.add(p.id);
      void fetchProjectSheetCounts(p.id)
        .then((entry) => {
          setCounts((prev) => ({ ...prev, [p.id]: entry }));
        })
        .catch(() => undefined);
    }
  }, [projects]);

  const dismissWelcome = () => {
    if (welcome) window.localStorage.setItem(welcome.key, '1');
    setDismissedWelcomeKey(welcome?.key ?? null);
  };

  const createAndOpen = (name: string) => {
    const trimmed = name.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setError(null);
    setRemediation(null);
    const startedAt = performance.now();
    createProject(trimmed)
      .then((project) => {
        sendProductTelemetry({
          type: 'Project.created',
          properties: { creationKind: 'blank', requestDuration: durationBucket(performance.now() - startedAt) },
        }, project.id);
        onOpen(project);
      })
      .catch((e: Error) => {
        sendProductTelemetry({
          type: 'Project.createFailed',
          properties: {
            creationKind: 'blank',
            requestDuration: durationBucket(performance.now() - startedAt),
            failureCategory: failureCategory(e),
          },
        });
        setError(e.message);
      })
      .finally(() => setBusy(false));
  };

  // Idempotent sample-project onboarding: reuse an existing "Sample
  // project" or create+seed one, cleaning up only a project THIS click created
  // if seeding fails.
  const trySampleAndOpen = () => {
    if (busy || sampleBusy) return;
    setSampleBusy(true);
    setError(null);
    const startedAt = performance.now();
    const ensureProject: Promise<{ project: ProjectInfo; reused: boolean }> = listProjects()
      .catch(() => projects ?? [])
      .then((list) => list.find((p) => p.name === SAMPLE_PROJECT_NAME) ?? null)
      .then((existing) =>
        existing
          ? { project: existing, reused: true }
          : createProject(SAMPLE_PROJECT_NAME)
              .then((project) => ({ project, reused: false }))
              .catch((e: Error) => {
                throw new Error(`Could not create the sample project: ${e.message}`);
              }),
      );
    ensureProject
      .then(({ project, reused }) =>
        seedSampleProject(project.id)
          .then(() => ({ project, reused }))
          .catch(async (e: Error) => {
            if (!reused) await deleteProject(project.id, project.name).catch(() => undefined);
            throw new Error(`Could not set up the sample project: ${e.message}`);
          }),
      )
      .then(({ project, reused }) => {
        if (!reused) {
          sendProductTelemetry({
            type: 'Project.created',
            properties: { creationKind: 'sample', requestDuration: durationBucket(performance.now() - startedAt) },
          }, project.id);
        }
        onOpen(project);
      })
      .catch((e: Error) => {
        sendProductTelemetry({
          type: 'Project.createFailed',
          properties: {
            creationKind: 'sample',
            requestDuration: durationBucket(performance.now() - startedAt),
            failureCategory: failureCategory(e),
          },
        });
        setError(e.message);
      })
      .finally(() => setSampleBusy(false));
  };

  const setFlag = (project: ProjectInfo, patch: { starred?: boolean; archived?: boolean }) => {
    // Optimistic: reflect the flag locally, then reconcile with the server.
    setProjects((prev) =>
      (prev ?? []).map((p) => (p.id === project.id ? { ...p, ...patch } : p)),
    );
    void updateProject(project.id, patch)
      .then(() => reload())
      .catch(() => reload());
  };

  // Rename from the card ⋯ menu: reuses the same additive PATCH updateProject
  // the Project General settings page's Name field already rides.
  const renameProject = (project: ProjectInfo, nextName: string) => {
    setProjects((prev) =>
      (prev ?? []).map((p) => (p.id === project.id ? { ...p, name: nextName } : p)),
    );
    void updateProject(project.id, { name: nextName })
      .then(() => reload())
      .catch((e: Error) => {
        setError(e.message);
        reload();
      });
  };

  const removeProject = (project: ProjectInfo) => {
    void deleteProject(project.id, project.name)
      .then(() => reload())
      .catch((e: Error) => setError(e.message));
  };

  return {
    projects,
    counts,
    welcome,
    error,
    remediation,
    busy,
    sampleBusy,
    reload,
    setError,
    dismissWelcome,
    createAndOpen,
    trySampleAndOpen,
    setFlag,
    renameProject,
    removeProject,
  };
}

/** The route-home projects screen. */
export function HomeScreen({ onOpen }: HomeScreenProps) {
  const { identityMode, me } = useShellIdentity();
  const {
    projects,
    counts,
    welcome,
    error,
    remediation,
    busy,
    sampleBusy,
    dismissWelcome,
    createAndOpen,
    trySampleAndOpen,
    setFlag,
    renameProject,
    removeProject,
  } = useHomeProjects(onOpen, me);
  const [scope, setScope] = useState<NavScope>('home');
  const [view, setView] = useState<HomeView>('grid');
  const [filter, setFilter] = useState('');
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const filterInputRef = useRef<HTMLInputElement>(null);
  const instance = useInstanceIdentity();

  const scoped = useMemo(() => {
    const all = projects ?? [];
    const byScope = all.filter((p) => {
      if (scope === 'archive') return p.archived;
      if (scope === 'starred') return p.starred && !p.archived;
      return !p.archived; // home / recent
    });
    const q = filter.trim().toLowerCase();
    const filtered = q
      ? byScope.filter((p) => p.name.toLowerCase().includes(q))
      : byScope;
    return filtered;
  }, [projects, scope, filter]);

  const doCreate = () => {
    createAndOpen(name);
  };

  const startRename = (project: ProjectInfo) => {
    setMenuFor(null);
    setRenamingId(project.id);
    setRenameValue(project.name);
  };

  const cancelRename = () => {
    setRenamingId(null);
    setRenameValue('');
  };

  const submitRename = (project: ProjectInfo) => {
    const trimmed = renameValue.trim();
    if (!trimmed || trimmed === project.name) {
      cancelRename();
      return;
    }
    cancelRename();
    renameProject(project, trimmed);
  };

  const toggleFlag = (
    project: ProjectInfo,
    patch: { starred?: boolean; archived?: boolean },
  ) => {
    setMenuFor(null);
    setFlag(project, patch);
  };

  const doDelete = (project: ProjectInfo) => {
    setMenuFor(null);
    if (!window.confirm(`Delete "${project.name}"? This removes the project bundle permanently.`)) {
      return;
    }
    removeProject(project);
  };

  const count = scoped.length;
  const scopeLabel =
    scope === 'archive' ? 'Archived' : scope === 'starred' ? 'Starred' : 'Your projects';

  const startCreate = () => {
    setCreating(true);
    setTimeout(() => {
      (document.querySelector(
        '[data-testid="new-project-name"]',
      ) as HTMLInputElement | null)?.focus();
    }, 0);
  };

  return (
    <div className="home-screen" data-testid="home-screen">
      <ChromeBarShell>
        <button
          type="button"
          className="chrome-command-pill"
          data-testid="home-command-pill"
          onClick={() => filterInputRef.current?.focus()}
        >
          <Search size={13} />
          <span className="chrome-command-label">Search projects</span>
          <span className="chrome-command-kbd">⌘K</span>
        </button>
        <AccountMenu triggerTestId="home-account" identityMode={identityMode} me={me} />
      </ChromeBarShell>

      <div className="home-body">
        <HomeNav scope={scope} onScopeChange={setScope} />

        <main className="home-main">
          {welcome && (
            <div className="account-note" data-testid="instance-welcome-message">
              {welcome.message}
              <button type="button" className="mini-btn" onClick={dismissWelcome}>
                Got it
              </button>
            </div>
          )}

          <HomeToolbar
            scopeLabel={scopeLabel}
            count={count}
            view={view}
            onViewChange={setView}
            filter={filter}
            filterInputRef={filterInputRef}
            onFilterChange={setFilter}
            onNewProject={startCreate}
            onTrySample={trySampleAndOpen}
            sampleBusy={sampleBusy}
            busy={busy}
            creating={creating}
            name={name}
            onNameChange={setName}
            onCreateSubmit={doCreate}
            onCreateCancel={() => {
              setCreating(false);
              setName('');
            }}
          />

          {error && (
            <div className="picker-error" data-testid="picker-error">
              {error}
              {remediation && (
                <div className="picker-error-remediation" data-testid="picker-error-remediation">
                  {remediation}
                </div>
              )}
              <SupportContactNote supportContact={instance.support_contact} />
            </div>
          )}

          {projects === null ? (
            <div className="picker-empty">Loading…</div>
          ) : (
            <div
              className={`home-grid home-grid-${view}`}
              data-testid="project-list"
              data-view={view}
            >
              {scoped.map((p) => (
                <HomeProjectCard
                  key={p.id}
                  project={p}
                  counts={counts[p.id]}
                  menuOpen={menuFor === p.id}
                  onToggleMenu={() => setMenuFor((cur) => (cur === p.id ? null : p.id))}
                  onOpen={() => onOpen(p)}
                  onStar={() => toggleFlag(p, { starred: !p.starred })}
                  onArchive={() => toggleFlag(p, { archived: !p.archived })}
                  onDelete={() => doDelete(p)}
                  renaming={renamingId === p.id}
                  renameValue={renamingId === p.id ? renameValue : p.name}
                  onStartRename={() => startRename(p)}
                  onRenameChange={setRenameValue}
                  onRenameSubmit={() => submitRename(p)}
                  onRenameCancel={cancelRename}
                />
              ))}
              {scope !== 'archive' && (
                <button
                  type="button"
                  className="home-new-tile"
                  data-testid="home-new-project-tile"
                  onClick={startCreate}
                >
                  <FolderPlus size={20} />
                  <span>New project</span>
                  <span className="home-new-tile-hint">Import data or start empty</span>
                </button>
              )}
              {scoped.length === 0 && (
                <div className="home-empty" data-testid="home-empty">
                  {scope === 'archive'
                    ? 'No archived projects.'
                    : scope === 'starred'
                      ? 'No starred projects yet — star one from its ⋯ menu.'
                      : 'No projects yet — create one to get started.'}
                </div>
              )}
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

// Left scope-nav (Home / Recent / Starred / Archive + Workspace settings).
// TIER HONESTY: no "Shared with me" / TEAMS group on the local tier — those
// render only when the auth tier provides collaborators. Extracted from
// HomeScreen.
function HomeNav({
  scope,
  onScopeChange,
}: {
  scope: NavScope;
  onScopeChange(scope: NavScope): void;
}) {
  const { identityMode } = useShellIdentity();
  return (
    <nav className="home-nav" data-testid="home-nav">
      <button
        type="button"
        className={`home-nav-item${scope === 'home' ? ' active' : ''}`}
        data-testid="home-nav-home"
        onClick={() => onScopeChange('home')}
      >
        <Home size={14} />
        <span>Home</span>
      </button>
      <button
        type="button"
        className={`home-nav-item${scope === 'recent' ? ' active' : ''}`}
        data-testid="home-nav-recent"
        onClick={() => onScopeChange('recent')}
      >
        <Clock size={14} />
        <span>Recent</span>
      </button>
      <button
        type="button"
        className={`home-nav-item${scope === 'starred' ? ' active' : ''}`}
        data-testid="home-nav-starred"
        onClick={() => onScopeChange('starred')}
      >
        <Star size={14} />
        <span>Starred</span>
      </button>
      <button
        type="button"
        className={`home-nav-item${scope === 'archive' ? ' active' : ''}`}
        data-testid="home-nav-archive"
        onClick={() => onScopeChange('archive')}
      >
        <Archive size={14} />
        <span>Archive</span>
      </button>
      <div className="home-nav-spacer" />
      <button
        type="button"
        className="home-nav-item home-nav-workspace"
        data-testid="home-nav-workspace-settings"
        onClick={() =>
          // Every ORGANIZATION section renders "Hosted organization settings
          // are unavailable in local mode", so landing there locally means the
          // one page in Settings that is dead. Local mode's workspace-level
          // config — provider keys and the local AI server URL — is
          // personal.ai-providers (`visibility: 'local-only'`), so that is
          // where the same button lands.
          navigate(
            identityMode
              ? { kind: 'settings', scope: 'organization', section: 'ai-providers' }
              : { kind: 'settings', scope: 'personal', section: 'ai-providers' },
          )
        }
      >
        <Settings size={14} />
        <span>Workspace settings</span>
      </button>
    </nav>
  );
}

// The header controls above the project grid: scope title + count, the
// grid/list view switcher, New project + Try-the-sample buttons, the filter
// input, and the inline create-project form. Extracted from HomeScreen.
function HomeToolbar({
  scopeLabel,
  count,
  view,
  onViewChange,
  filter,
  filterInputRef,
  onFilterChange,
  onNewProject,
  onTrySample,
  sampleBusy,
  busy,
  creating,
  name,
  onNameChange,
  onCreateSubmit,
  onCreateCancel,
}: {
  scopeLabel: string;
  count: number;
  view: HomeView;
  onViewChange(view: HomeView): void;
  filter: string;
  filterInputRef: RefObject<HTMLInputElement>;
  onFilterChange(value: string): void;
  onNewProject(): void;
  onTrySample(): void;
  sampleBusy: boolean;
  busy: boolean;
  creating: boolean;
  name: string;
  onNameChange(value: string): void;
  onCreateSubmit(): void;
  onCreateCancel(): void;
}) {
  return (
    <>
      <div className="home-main-head">
        <div className="home-title-row">
          <h1 className="home-projects-header" data-testid="home-projects-header">
            {scopeLabel}
          </h1>
          <span className="home-projects-count" data-testid="home-projects-count">
            {count} {count === 1 ? 'project' : 'projects'}
          </span>
        </div>
        <div className="home-actions-row">
          <SegmentedToggle
            className="segmented-toolbar home-view-switcher"
            fullWidth={false}
            testId="home-view-switcher"
            ariaLabel="Project view"
            value={view}
            onValueChange={(next) => onViewChange(next as HomeView)}
            buttonTestId={(value) => `home-view-${value}`}
            options={[
              { value: 'grid', label: 'Grid' },
              { value: 'list', label: 'List' },
            ]}
          />
          <button
            type="button"
            className="btn btn-primary home-new-project"
            data-testid="home-new-project"
            onClick={onNewProject}
          >
            <Plus size={13} /> New project
          </button>
        </div>
      </div>

      <div className="home-filter-row">
        <div className="home-filter-input">
          <Search size={13} />
          <input
            ref={filterInputRef}
            className="home-filter"
            aria-label="Filter projects"
            placeholder="Filter projects…"
            value={filter}
            data-testid="home-filter"
            onChange={(e) => onFilterChange(e.target.value)}
          />
        </div>
        <button
          type="button"
          className="btn home-sample-btn"
          data-testid="try-sample-project"
          onClick={onTrySample}
          disabled={busy || sampleBusy}
        >
          <FlaskConical size={13} />{' '}
          {sampleBusy ? 'Setting up the sample…' : 'Try the sample project'}
        </button>
      </div>

      {creating && (
        <form
          className="home-create-row"
          onSubmit={(e) => {
            e.preventDefault();
            onCreateSubmit();
          }}
        >
          <input
            aria-label="New project name"
            className="form-input"
            value={name}
            placeholder="new project name"
            data-testid="new-project-name"
            onChange={(e) => onNameChange(e.target.value)}
          />
          <button
            type="submit"
            className="btn btn-primary"
            data-testid="create-project"
            disabled={!name.trim() || busy}
          >
            <Plus size={13} /> Create
          </button>
          <button
            type="button"
            className="btn"
            onClick={onCreateCancel}
          >
            Cancel
          </button>
        </form>
      )}
    </>
  );
}

function HomeProjectCard({
  project,
  counts,
  menuOpen,
  onToggleMenu,
  onOpen,
  onStar,
  onArchive,
  onDelete,
  renaming,
  renameValue,
  onStartRename,
  onRenameChange,
  onRenameSubmit,
  onRenameCancel,
}: {
  project: ProjectInfo;
  counts: CardCounts | undefined;
  menuOpen: boolean;
  onToggleMenu(): void;
  onOpen(): void;
  onStar(): void;
  onArchive(): void;
  onDelete(): void;
  renaming: boolean;
  renameValue: string;
  onStartRename(): void;
  onRenameChange(value: string): void;
  onRenameSubmit(): void;
  onRenameCancel(): void;
}) {
  const opened = formatRelativeTime(project.updated_at);
  // The card ⋯ menu: onToggleMenu toggles, so firing it while open
  // closes; the trigger testid is the popover's ignoreSelector so its own click
  // is not an "outside" dismiss.
  const menuTriggerRef = useRef<HTMLButtonElement>(null);
  const menuPopRef = useRef<HTMLDivElement>(null);
  const menuTestId = `home-card-menu-${project.id}`;
  useNativePopover(menuPopRef, onToggleMenu, {
    enabled: menuOpen,
    ignoreSelector: `[data-testid="${menuTestId}"]`,
  });
  // Fixed-position anchor now that the menu is a top-layer element — it no
  // longer inherits placement from `.home-card-overflow`'s CSS positioned
  // ancestor (styles.css `.home-card-menu`, right-aligned under the trigger).
  const menuPos = useAnchoredPosition(menuTriggerRef, {
    enabled: menuOpen,
    align: 'right',
    width: 150,
    gap: 4,
  });

  // Focus the rename input when the form appears — an imperative ref+effect
  // (react-doctor's no-autofocus rule: the JSX `autoFocus` prop moves focus
  // unconditionally on every render pass, disorienting screen reader/keyboard
  // users; this only fires the one time `renaming` flips true).
  const renameInputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (renaming) renameInputRef.current?.focus();
  }, [renaming]);

  return (
    <div className="home-card" data-testid={`project-${project.id}`}>
      {renaming ? (
        <div className="home-card-open home-card-open-renaming">
          <span className="home-card-cover" style={coverStyle(project)}>
            <span className="home-card-glyph">{coverGlyph(project)}</span>
          </span>
          <form
            className="home-card-body home-card-rename-form"
            onSubmit={(e) => {
              e.preventDefault();
              onRenameSubmit();
            }}
          >
            <input
              ref={renameInputRef}
              className="form-input home-card-rename-input"
              aria-label={`Rename ${project.name}`}
              data-testid={`home-card-rename-input-${project.id}`}
              value={renameValue}
              onChange={(e) => onRenameChange(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') {
                  e.preventDefault();
                  onRenameCancel();
                }
              }}
            />
            <div className="home-card-rename-actions">
              <button
                type="submit"
                className="btn btn-primary"
                data-testid={`home-card-rename-save-${project.id}`}
              >
                Save
              </button>
              <button
                type="button"
                className="btn"
                data-testid={`home-card-rename-cancel-${project.id}`}
                onClick={onRenameCancel}
              >
                Cancel
              </button>
            </div>
          </form>
        </div>
      ) : (
        <button type="button" className="home-card-open" onClick={onOpen}>
          <span className="home-card-cover" style={coverStyle(project)}>
            <span className="home-card-glyph">{coverGlyph(project)}</span>
            {project.archived && <span className="home-card-chip">archived</span>}
          </span>
          <span className="home-card-body">
            <span className="home-card-titlerow">
              {project.starred && <Star size={12} className="home-card-star" />}
              <span className="home-card-title">{project.name}</span>
              {!!project.pending_review_count && (
                <span
                  className="badge home-card-pending"
                  data-testid={`project-${project.id}-pending`}
                >
                  {project.pending_review_count}
                </span>
              )}
            </span>
            <span
              className="home-card-meta"
              data-testid={`project-${project.id}-meta`}
            >
              {counts && (
                <span className="home-card-counts">{`${counts.sheets} ${
                  counts.sheets === 1 ? 'sheet' : 'sheets'
                } · ${counts.rows} ${counts.rows === 1 ? 'row' : 'rows'}`}</span>
              )}
              {opened && (
                <span data-testid={`project-${project.id}-updated`}>
                  {counts ? ` · opened ${opened}` : `opened ${opened}`}
                </span>
              )}
              {/* Allowlisted, not migrated to PanelLoading —
                  an inline "…" ellipsis inside a running text line, not a block
                  "Loading…" message. */}
              {!counts && !opened && <span className="home-card-meta-loading">…</span>}
            </span>
          </span>
        </button>
      )}
      <div className="home-card-overflow">
        <button
          type="button"
          ref={menuTriggerRef}
          className="home-card-menu-btn"
          data-testid={menuTestId}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          title="Project actions"
          onClick={onToggleMenu}
        >
          <MoreHorizontal size={15} />
        </button>
        {menuOpen && (
          <MenuPop
            ref={menuPopRef}
            className="home-card-menu"
            // Reset the UA popover centering so the JS-anchored top-layer
            // placement below applies instead.
            style={
              menuPos
                ? { position: 'fixed', inset: 'auto', top: menuPos.top, bottom: menuPos.bottom, left: menuPos.left, right: 'auto', width: menuPos.width, margin: 0 }
                : { position: 'fixed', visibility: 'hidden' }
            }
          >
            <button
              type="button"
              className="menu-item"
              role="menuitem"
              data-testid={`home-card-rename-${project.id}`}
              onClick={onStartRename}
            >
              <Pencil size={12} className="menu-item-icon" />
              <span className="menu-item-name">Rename</span>
            </button>
            <button
              type="button"
              className="menu-item"
              role="menuitem"
              data-testid={`home-card-star-${project.id}`}
              onClick={onStar}
            >
              <Star size={12} className="menu-item-icon" />
              <span className="menu-item-name">{project.starred ? 'Unstar' : 'Star'}</span>
            </button>
            <button
              type="button"
              className="menu-item"
              role="menuitem"
              data-testid={`home-card-archive-${project.id}`}
              onClick={onArchive}
            >
              {project.archived ? (
                <ArchiveRestore size={12} className="menu-item-icon" />
              ) : (
                <Archive size={12} className="menu-item-icon" />
              )}
              <span className="menu-item-name">
                {project.archived ? 'Unarchive' : 'Archive'}
              </span>
            </button>
            <div className="menu-sep" />
            <button
              type="button"
              className="menu-item menu-item-danger"
              role="menuitem"
              data-testid={`home-card-delete-${project.id}`}
              onClick={onDelete}
            >
              <Trash2 size={12} className="menu-item-icon" />
              <span className="menu-item-name">Delete</span>
            </button>
          </MenuPop>
        )}
      </div>
    </div>
  );
}
