import './App.css';
import { Button } from './components/Button';
import { lazy, Suspense, useEffect, useRef, useState, type ReactNode } from 'react';
import { resumePendingSignIn } from './components/authReturnTo';
import { listProjects, type ProjectInfo } from './api/open';
import { PanelEmpty, PanelLoading } from './components/PanelPrimitives';
import { HomeScreen } from './components/HomeScreen';
import { ReplayModeBanner } from './components/ReplayModeBanner';
import { ApplicationGuidance } from './components/ApplicationGuidance';
import { setSampleGuideArrival } from './workbench/sampleGuideArrival';
import { readSettingsProjectContext } from './settings/settingsProjectContext';
import { ProductTelemetryProvider } from './telemetry/ProductTelemetryProvider';
import { sendProductTelemetry } from './telemetry/productTelemetry';
import { navigate, useRoute, type RoutePanel, type SettingsRoute } from './routes';
import { DiagnosticReportButton } from './workspace/DiagnosticReport';
import { WorkspaceStoresProvider } from './bind/WorkspaceStoresProvider';
import { useShellIdentity } from './shellIdentity';
import { useEditionModule } from './editions/module';
import { Workspace } from './workspace/Workspace';

const LazySignIn = lazy(() => (
  import('./components/SignIn').then(({ SignIn }) => ({ default: SignIn }))
));

const LazySettingsWorkspace = lazy(() => (
  import('./settings/SettingsWorkspace').then(({ SettingsWorkspace }) => ({
    default: SettingsWorkspace,
  }))
));

const LazyAdminPage = lazy(() => (
  import('./components/AdminPage').then(({ AdminPage }) => ({ default: AdminPage }))
));

function lazyPage(node: ReactNode) {
  return (
    <Suspense fallback={<PanelLoading className="panel-loading-page" label="Loading…" />}>
      {node}
    </Suspense>
  );
}

const openHome = () => (
  <HomeScreen onOpen={(p, options) => {
    setSampleGuideArrival(options?.openGuide ? p.id : null);
    navigate({ kind: 'project', projectId: p.id });
  }} />
);

const openAdminUnavailable = () => (
  <div className="picker-screen" data-testid="admin-unavailable">
    <div className="picker-card">
      <PanelEmpty>Admin isn&apos;t available in local mode.</PanelEmpty>
      <Button
        type="button"

        data-testid="admin-unavailable-home-link"
        style={{ marginTop: 12 }}
        onClick={() => navigate({ kind: 'picker' })}
      >
        ← All projects
      </Button>
    </div>
  </div>
);

export default function App() {
  const route = useRoute();
  const { routes: editionRoutes } = useEditionModule();
  const activeRoute = editionRoutes.find((candidate) => candidate.path === window.location.pathname);
  const ActiveRouteComponent = activeRoute ? activeRoute.component : undefined;

  const { identityMode, me, resolved: identityResolved } = useShellIdentity();
  const needsSignIn = identityMode && identityResolved && me === null;

  useEffect(() => {
    if (!identityMode || !identityResolved || !me) return;
    const target = resumePendingSignIn(window.location);
    if (target) window.location.replace(target);
  }, [identityMode, identityResolved, me]);

  if (activeRoute?.access === 'public') {
    if (ActiveRouteComponent) return lazyPage(<ActiveRouteComponent />);
    if (activeRoute.handler) {
      const contribution = activeRoute.handler();
      if (contribution) return lazyPage(contribution);
    }
  }
  if (!identityResolved) {
    return <div className="picker-screen" />;
  }
  if (needsSignIn) {
    return lazyPage(<LazySignIn />);
  }
  const withProductTelemetry = (node: ReactNode) => (
    <ProductTelemetryProvider>{node}</ProductTelemetryProvider>
  );
  if (activeRoute?.access === 'authenticated' && ActiveRouteComponent) {
    return withProductTelemetry(lazyPage(<ActiveRouteComponent />));
  }
  if (activeRoute?.access === 'authenticated' && activeRoute.handler) {
    const contribution = activeRoute.handler();
    if (contribution) return withProductTelemetry(lazyPage(contribution));
  }
  const withDiagnostics = (node: ReactNode) => (
    <div className="app-shell">
      <ReplayModeBanner />
      <div className="app-shell-body">{node}</div>
      {identityMode && route.kind !== 'project' && (
        <footer className="status-bar diagnostic-footer">
          <DiagnosticReportButton />
        </footer>
      )}
    </div>
  );
  const content = (() => {
    switch (route.kind) {
    case 'settings':
      if (route.scope === 'project' && route.projectId) {
        return withDiagnostics(
          <ProjectSettingsRoute
            key={`settings-${route.projectId}`}
            route={route}
            projectId={route.projectId}
            identityMode={identityMode}
          />,
        );
      }
      return withDiagnostics(lazyPage(
        <LazySettingsWorkspace route={route} identityMode={identityMode} />,
      ));
    case 'admin':
      return editionRoutes.some((candidate) => candidate.id.startsWith('admin-'))
        ? withDiagnostics(lazyPage(<LazyAdminPage />))
        : withDiagnostics(openAdminUnavailable());
    case 'project':
      return withDiagnostics(
        <ProjectRoute
          key={route.projectId}
          projectId={route.projectId}
          sheetId={route.sheetId}
          actionKind={route.actionKind}
          review={route.review}
          panel={route.panel}
        />,
      );
    default:
      return withDiagnostics(openHome());
    }
  })();
  const walkthroughProjectId = route.kind === 'project'
    ? route.projectId
    : route.kind === 'settings'
      ? route.projectId ?? readSettingsProjectContext()?.id
      : undefined;
  return withProductTelemetry(
    <ApplicationGuidance projectId={walkthroughProjectId}>
      {content}
    </ApplicationGuidance>,
  );
}

type ProjectRouteState = {
  projectId: string | null;
  project: ProjectInfo | null;
  error: string | null;
};

function ProjectSettingsRoute({
  route,
  projectId,
  identityMode,
}: {
  route: SettingsRoute;
  projectId: string;
  identityMode: boolean;
}) {
  const [state, setState] = useState<ProjectRouteState>({ projectId: null, project: null, error: null });

  useEffect(() => {
    let alive = true;
    void (async () => {
      let nextState: ProjectRouteState;
      try {
        const list = await listProjects();
        const p = list.find((x) => x.id === projectId);
        if (!p) {
          nextState = { projectId, project: null, error: `No project “${projectId}” in this workspace.` };
        } else {
          nextState = { projectId, project: p, error: null };
        }
      } catch (e) {
        const message = e instanceof Error ? e.message : String(e);
        nextState = { projectId, project: null, error: `Cannot reach the frisket server: ${message}` };
      }
      if (alive) setState(nextState);
    })();
    return () => {
      alive = false;
    };
  }, [projectId]);

  if (state.projectId !== projectId) {
    return <PanelLoading className="panel-loading-page" label="Loading project settings…" />;
  }
  if (state.error) {
    return lazyPage(
      <LazySettingsWorkspace
        route={route}
        identityMode={identityMode}
        routeError={state.error}
      />,
    );
  }
  if (!state.project) {
    return <PanelLoading className="panel-loading-page" label="Loading project settings…" />;
  }
  return lazyPage(
    <WorkspaceStoresProvider projectId={state.project.id}>
      <LazySettingsWorkspace
        route={route}
        project={state.project}
        identityMode={identityMode}
      />
    </WorkspaceStoresProvider>,
  );
}

export function ProjectRoute({
  projectId,
  sheetId,
  actionKind,
  review,
  panel,
}: {
  projectId: string;
  sheetId?: string;
  actionKind?: string;
  review?: boolean;
  panel?: RoutePanel;
}) {
  const { identityMode } = useShellIdentity();
  const [state, setState] = useState<ProjectRouteState>({ projectId: null, project: null, error: null });

  useEffect(() => {
    let alive = true;
    void (async () => {
      let nextState: ProjectRouteState;
      try {
        const list = await listProjects();
        const p = list.find((x) => x.id === projectId);
        if (!p) {
          nextState = { projectId, project: null, error: `No project “${projectId}” in this workspace.` };
        } else {
          nextState = { projectId, project: p, error: null };
        }
      } catch (e) {
        const message = e instanceof Error ? e.message : String(e);
        nextState = { projectId, project: null, error: `Cannot reach the frisket server: ${message}` };
      }
      if (alive) setState(nextState);
    })();
    return () => {
      alive = false;
    };
  }, [projectId]);

  const openedProjectRef = useRef<string | null>(null);
  useEffect(() => {
    if (state.projectId !== projectId || !state.project || openedProjectRef.current === projectId) return;
    openedProjectRef.current = projectId;
    sendProductTelemetry({ type: 'Project.opened', properties: {} }, projectId);
  }, [projectId, state.project, state.projectId]);

  if (state.error) {
    return (
      <div className="picker-screen">
        <div className="picker-card">
          <div className="picker-error" data-testid="project-route-error">{state.error}</div>
          {identityMode && <DiagnosticReportButton />}
          <Button
            type="button"

            style={{ marginTop: 12 }}
            onClick={() => navigate({ kind: 'picker' })}
          >
            ← All projects
          </Button>
        </div>
      </div>
    );
  }
  if (!state.project) {
    return <>
      <PanelLoading className="panel-loading-page" label="Loading project…" />
      {identityMode && <footer className="status-bar diagnostic-footer"><DiagnosticReportButton /></footer>}
    </>;
  }
  return (
    <WorkspaceStoresProvider projectId={state.project.id}>
      <Workspace
        key={state.project.id}
        project={state.project}
        routeSheetId={sheetId}
        routeActionKind={actionKind}
        routeReview={review}
        routePanel={panel}
      />
    </WorkspaceStoresProvider>
  );
}
