import { useEffect, useMemo, useState } from 'react';
import { ArrowLeft, Search } from 'lucide-react';
import { listProjects, type ProjectInfo } from '../api/open';
import { navigate, type SettingsRoute } from '../routes';
import { ChromeBarShell } from '../workbench/ChromeBar';
import { PanelSelect } from '../components/PanelSelect';
import { SectionFrame, SettingsSectionRenderer } from './SettingsSections';
import {
  settingsSectionId,
  type SettingsSectionDefinition,
} from './settingsRegistry';
import {
  groupedSettingsNavItems,
  settingsItemAvailability,
  type SettingsNavItem,
} from './settingsNav';
import {
  readSettingsProjectContext,
  writeSettingsProjectContext,
} from './settingsProjectContext';
import { useEditionModule } from '../editions/module';
import type { ActiveSettingsSection } from './openSettingsRegistry';

export interface SettingsWorkspaceProps {
  route: SettingsRoute;
  project?: ProjectInfo | null;
  identityMode: boolean;
  routeError?: string | null;
}

function routeForDefinition(
  definition: SettingsSectionDefinition,
  project?: ProjectInfo | null,
): SettingsRoute {
  return {
    kind: 'settings',
    scope: definition.scope,
    section: definition.section,
    projectId: definition.scope === 'project' ? project?.id : undefined,
  };
}

function mergeProjectOptions(
  currentProject: ProjectInfo | null | undefined,
  projects: ProjectInfo[] | null,
): ProjectInfo[] {
  const options = projects ? [...projects] : [];
  if (currentProject && !options.some((project) => project.id === currentProject.id)) {
    options.unshift(currentProject);
  }
  return options;
}

// The standard app chrome (ChromeBarShell — the SAME top strip the project
// workspace and Home render, workbench/ChromeBar.tsx) so settings never feels
// like a separate app with no way out. The back link targets `navProject`
// when known (the project the user came from, via settingsProjectContext —
// written by AccountMenu.tsx's goPersonal when entering from a project's
// chrome bar, or by SettingsWorkspace's own effect below when scope is
// 'project'); with no project in scope it falls back to the project picker.
function SettingsChromeBar({ navProject }: { navProject?: ProjectInfo | null }) {
  // jsx-no-jsx-as-prop: ChromeBarShell's brandExtra prop was literal JSX
  // built fresh every render; memoized over its one actual dependency
  // (navProject).
  const brandExtra = useMemo(
    () =>
      navProject ? (
        <button
          type="button"
          className="settings-chrome-back"
          data-testid="settings-return-project"
          onClick={() => navigate({ kind: 'project', projectId: navProject.id })}
        >
          <ArrowLeft size={14} />
          <span>Back to {navProject.name}</span>
        </button>
      ) : (
        <button
          type="button"
          className="settings-chrome-back"
          data-testid="settings-return-home"
          onClick={() => navigate({ kind: 'picker' })}
        >
          <ArrowLeft size={14} />
          <span>Back to projects</span>
        </button>
      ),
    [navProject],
  );
  return <ChromeBarShell brandExtra={brandExtra}>{null}</ChromeBarShell>;
}

function SettingsProjectSwitcher({
  route,
  currentProject,
  projects,
  error,
}: {
  route: SettingsRoute;
  currentProject?: ProjectInfo | null;
  projects: ProjectInfo[] | null;
  error: string | null;
}) {
  const projectOptions = useMemo(
    () => mergeProjectOptions(currentProject, projects),
    [currentProject, projects],
  );
  const currentProjectId =
    currentProject?.id ?? (route.scope === 'project' ? route.projectId ?? '' : '');
  const selectedValue = projectOptions.some((project) => project.id === currentProjectId)
    ? currentProjectId
    : '';
  const placeholder =
    projects === null
      ? 'Loading projects...'
      : projectOptions.length === 0
        ? 'No projects'
        : 'Choose project...';
  const targetSection = route.scope === 'project' ? route.section : 'general';

  return (
    <label className="settings-project-switcher">
      <span>Project</span>
      <PanelSelect
        className="settings-project-select"
        data-testid="settings-project-switcher"
        aria-label="Project settings project"
        value={selectedValue}
        disabled={projectOptions.length === 0}
        onChange={(event) => {
          const selectedProjectId = event.target.value;
          if (!selectedProjectId) return;
          const selectedProject = projectOptions.find((project) => project.id === selectedProjectId);
          if (selectedProject) writeSettingsProjectContext(selectedProject);
          navigate({
            kind: 'settings',
            projectId: selectedProjectId,
            scope: 'project',
            section: targetSection,
          });
        }}
      >
        <option value="">{placeholder}</option>
        {projectOptions.map((project) => (
          <option key={project.id} value={project.id}>
            {project.name}
          </option>
        ))}
      </PanelSelect>
      {error && <span className="settings-project-error">{error}</span>}
    </label>
  );
}

function SettingsNavButton({
  item,
  active,
  project,
}: {
  item: SettingsNavItem;
  active: boolean;
  project?: ProjectInfo | null;
}) {
  const { definition } = item;
  const testId = `settings-nav-${definition.scope}-${definition.section}`;
  return (
    <button
      type="button"
      className={`settings-nav-item${active ? ' active' : ''}`}
      data-testid={testId}
      data-settings-section-id={definition.id}
      aria-current={active ? 'page' : undefined}
      disabled={item.disabled}
      title={item.disabledReason ?? definition.title}
      onClick={() => {
        navigate(routeForDefinition(definition, project));
      }}
    >
      <span className="settings-nav-title">{definition.title}</span>
      {item.disabledReason && (
        <span className="settings-nav-reason">{item.disabledReason}</span>
      )}
    </button>
  );
}

function SettingsDisabledSection({
  definition,
  reason,
}: {
  definition: SettingsSectionDefinition;
  reason: string;
}) {
  return (
    <SectionFrame
      definition={definition}
      summary={reason}
      testId={`settings-disabled-${definition.scope}-${definition.section}`}
      className="settings-section-disabled"
    >
      {definition.visibility === 'project' && (
        <button className="btn" type="button" onClick={() => navigate({ kind: 'picker' })}>
          Open a project
        </button>
      )}
    </SectionFrame>
  );
}

function SettingsInvalidSection({ route }: { route: SettingsRoute }) {
  return (
    <SectionFrame
      kicker={route.scope}
      title="Invalid settings section"
      summary={`${settingsSectionId(route.scope, route.section)} is not available.`}
      testId="settings-invalid-section"
    />
  );
}

function SettingsRouteError({ message }: { message: string }) {
  return (
    <SectionFrame
      kicker="Project"
      title="Project unavailable"
      summary={message}
      testId="project-settings-unavailable"
    >
      <button className="btn" type="button" onClick={() => navigate({ kind: 'picker' })}>
        Open a project
      </button>
    </SectionFrame>
  );
}

export function SettingsWorkspace({
  route,
  project,
  identityMode,
  routeError,
}: SettingsWorkspaceProps) {
  const [query, setQuery] = useState('');
  const [lastProject] = useState<ProjectInfo | null>(() => readSettingsProjectContext());
  const [projects, setProjects] = useState<ProjectInfo[] | null>(null);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const { settingsSections: editionSections } = useEditionModule();
  const definition: ActiveSettingsSection | null = editionSections.find(
    (candidate) => candidate.scope === route.scope && candidate.section === route.section,
  ) ?? null;
  useEffect(() => {
    if (!project) return;
    writeSettingsProjectContext(project);
  }, [project]);
  useEffect(() => {
    let alive = true;
    listProjects()
      .then((loaded) => {
        if (!alive) return;
        setProjects(loaded);
        setProjectsError(null);
      })
      .catch((error: Error) => {
        if (!alive) return;
        setProjects([]);
        setProjectsError(error.message);
      });
    return () => {
      alive = false;
    };
  }, []);
  const navProject = project ?? lastProject;
  const navProjectId = navProject?.id ?? route.projectId;
  const routeProjectId = project?.id ?? route.projectId;
  const groups = useMemo(
    () => groupedSettingsNavItems(
      { projectId: navProjectId, identityMode, query },
      editionSections,
    ),
    [editionSections, identityMode, navProjectId, query],
  );
  const currentAvailability = definition
    ? settingsItemAvailability(definition, {
      projectId: route.scope === 'project' ? routeProjectId : navProjectId,
      identityMode,
    })
    : null;
  const activeId = definition?.id ?? null;
  const contextLabel = project
    ? `${project.name} settings`
    : route.scope === 'organization'
      ? 'Organization settings'
      : route.scope === 'project'
        ? 'Project settings'
        : 'Personal settings';

  return (
    <div
      className="settings-workspace"
      data-testid="settings-workspace"
      data-route-scope={route.scope}
      data-route-section={route.section}
    >
      <SettingsChromeBar navProject={navProject} />
      <div className="settings-body">
        <aside className="settings-rail" aria-label="Settings navigation">
          <div className="settings-rail-brand">
            <div className="settings-context">
              <span className="settings-context-label">Settings</span>
              <strong data-testid="settings-context-name">{contextLabel}</strong>
            </div>
          </div>

          <SettingsProjectSwitcher
            route={route}
            currentProject={navProject}
            projects={projects}
            error={projectsError}
          />

          <label className="settings-search">
            <Search size={14} />
            <input
              data-testid="settings-search"
              value={query}
              placeholder="Search settings"
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>

          <nav className="settings-nav" data-testid="settings-nav" aria-label="Settings sections">
            {groups.length === 0 ? (
              <div className="settings-empty-state" data-testid="settings-search-empty">
                No matching settings
              </div>
            ) : (
              groups.map((group) => (
                <div className="settings-nav-group" key={group.group}>
                  <div className="settings-nav-group-label">{group.group}</div>
                  {group.items.map((item) => (
                    <SettingsNavButton
                      key={item.definition.id}
                      item={item}
                      active={item.definition.id === activeId}
                      project={navProject}
                    />
                  ))}
                </div>
              ))
            )}
          </nav>
        </aside>
        <main className="settings-content" data-testid="settings-content">
          {routeError ? (
            <SettingsRouteError message={routeError} />
          ) : !definition ? (
            <SettingsInvalidSection route={route} />
          ) : currentAvailability?.disabled ? (
            <SettingsDisabledSection
              definition={definition}
              reason={currentAvailability.disabledReason ?? 'This section is unavailable.'}
            />
          ) : (
            'handler' in definition ? (
              <SectionFrame definition={definition}>
                {definition.handler({ project, identityMode })}
              </SectionFrame>
            ) : (
              <SettingsSectionRenderer
                definition={definition}
                route={route}
                project={project}
                identityMode={identityMode}
              />
            )
          )}
        </main>
      </div>
    </div>
  );
}
