import { useEffect, useReducer, useRef, useState, type FormEvent } from 'react';
import { AlertTriangle, CheckCircle2, Info, XCircle } from 'lucide-react';
import type {
  WorkbenchPluginContributionSummary,
  WorkbenchPluginRuntimeIndex,
  WorkbenchPluginRuntimePlugin,
} from '../api/types';
import type { WorkbenchApiPort } from '../api/ports';
import { formatFrontendComponentBindings } from './pluginUiRuntime';

export interface PluginManagerProps {
  runtimeIndex: WorkbenchPluginRuntimeIndex | null;
  installLocalWorkbenchPlugin: WorkbenchApiPort['installLocalWorkbenchPlugin'];
  activateWorkbenchPlugin: WorkbenchApiPort['activateWorkbenchPlugin'];
  activateWorkbenchPluginBackend: WorkbenchApiPort['activateWorkbenchPluginBackend'];
  disableWorkbenchPlugin: WorkbenchApiPort['disableWorkbenchPlugin'];
  uninstallWorkbenchPlugin: WorkbenchApiPort['uninstallWorkbenchPlugin'];
  onRefresh: () => Promise<void> | void;
  mode?: 'settings' | 'status';
  allowLocalInstall?: boolean;
  canMutate?: boolean;
}

interface PluginManagerState {
  localPluginId: string;
  localSourceValue: string;
  pendingTrustPluginId: string | null;
  acceptedCapabilities: Record<string, boolean>;
  busyKey: string | null;
  status: string;
  error: string;
}

type PluginManagerAction =
  | { type: 'setPluginId'; value: string }
  | { type: 'setSourceValue'; value: string }
  | { type: 'beginTrust'; pluginId: string }
  | { type: 'toggleCapability'; capability: string; accepted: boolean }
  | { type: 'cancelTrust' }
  | { type: 'start'; key: string }
  | { type: 'success'; status: string }
  | { type: 'error'; message: string };

const initialState: PluginManagerState = {
  localPluginId: '',
  localSourceValue: '',
  pendingTrustPluginId: null,
  acceptedCapabilities: {},
  busyKey: null,
  status: '',
  error: '',
};

function reducer(state: PluginManagerState, action: PluginManagerAction): PluginManagerState {
  switch (action.type) {
    case 'setPluginId':
      return { ...state, localPluginId: action.value };
    case 'setSourceValue':
      return { ...state, localSourceValue: action.value };
    case 'beginTrust':
      return {
        ...state,
        pendingTrustPluginId: action.pluginId,
        acceptedCapabilities: {},
        error: '',
      };
    case 'toggleCapability':
      return {
        ...state,
        acceptedCapabilities: {
          ...state.acceptedCapabilities,
          [action.capability]: action.accepted,
        },
      };
    case 'cancelTrust':
      return { ...state, pendingTrustPluginId: null, acceptedCapabilities: {} };
    case 'start':
      return { ...state, busyKey: action.key, error: '' };
    case 'success':
      return {
        ...state,
        busyKey: null,
        pendingTrustPluginId: null,
        acceptedCapabilities: {},
        status: action.status,
      };
    case 'error':
      return { ...state, busyKey: null, error: action.message };
  }
}

function idsForKind(
  summary: WorkbenchPluginContributionSummary[],
  kind: string,
): string {
  return summary.find((item) => item.kind === kind)?.ids.join(' ') ?? '';
}

function contributionSummaryData(summary: WorkbenchPluginContributionSummary[]): string {
  return summary
    .flatMap((item) => item.ids.length > 0 ? [`${item.kind}:${item.ids.join(',')}`] : [])
    .join(' ');
}

function pluginCapabilities(plugin: WorkbenchPluginRuntimePlugin): string {
  return plugin.requires?.capabilities?.join(' ') ?? '';
}

function pluginSecrets(plugin: WorkbenchPluginRuntimePlugin): string {
  return plugin.requires?.secrets?.join(' ') ?? '';
}

function pluginSourceValue(plugin: WorkbenchPluginRuntimePlugin): string {
  const source = plugin.source ?? {};
  if (typeof source.value === 'string' && source.value) return source.value;
  if (typeof source.path === 'string' && source.path) return source.path;
  return '';
}

type PluginStatePresentation = {
  label: string;
  description: string;
  tone: 'positive' | 'attention' | 'neutral' | 'negative';
  Icon: typeof CheckCircle2;
};

function pluginStatePresentation(state: string): PluginStatePresentation {
  switch (state) {
    case 'enabled':
      return {
        label: 'Enabled',
        description: 'Available to this project and ready to contribute features.',
        tone: 'positive',
        Icon: CheckCircle2,
      };
    case 'installed':
      return {
        label: 'Needs review',
        description: 'Installed for the workspace. Review its access before enabling it for this project.',
        tone: 'attention',
        Icon: Info,
      };
    case 'disabled':
      return {
        label: 'Disabled',
        description: 'Kept in the project, but its contributions are not available.',
        tone: 'neutral',
        Icon: XCircle,
      };
    case 'failed':
      return {
        label: 'Failed',
        description: 'Frisket could not load this plugin. Review the failure below.',
        tone: 'negative',
        Icon: AlertTriangle,
      };
    case 'uninstalled':
      return {
        label: 'Uninstalled',
        description: 'Removed from this workspace. Source files on disk were not deleted.',
        tone: 'neutral',
        Icon: Info,
      };
    default:
      return {
        label: state || 'Unknown',
        description: 'The runtime reported a plugin state this version does not recognize.',
        tone: 'neutral',
        Icon: Info,
      };
  }
}

const CONTRIBUTION_LABELS: Record<string, string> = {
  action: 'Actions',
  column_type: 'Column types',
  importer: 'Importers',
  job_handler: 'Job handlers',
  operator: 'Operators',
  projection: 'Projections',
  workbench_panel: 'Panels',
  workbench_view: 'Views',
};

function contributionLabel(kind: string): string {
  const label = CONTRIBUTION_LABELS[kind] ?? kind.replace(/_/g, ' ');
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function pluginFailureDetail(plugin: WorkbenchPluginRuntimePlugin): string {
  const message = plugin.installFailure?.message;
  if (typeof message === 'string' && message) return message;
  return plugin.disabledReason ?? '';
}

function PluginStateBadge({ plugin }: { plugin: WorkbenchPluginRuntimePlugin }) {
  const state = pluginStatePresentation(plugin.installState);
  const StateIcon = state.Icon;
  return (
    <div className="plugin-manager-state">
      <span
        className={`plugin-manager-state-badge plugin-manager-state-badge-${state.tone}`}
        data-testid="plugin-manager-state-badge"
        data-state={plugin.installState}
      >
        <StateIcon size={13} aria-hidden />
        {state.label}
      </span>
      <span className="plugin-manager-state-description">{state.description}</span>
    </div>
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return String(error || 'Plugin operation failed');
}

export function PluginManager({
  runtimeIndex,
  installLocalWorkbenchPlugin,
  activateWorkbenchPlugin,
  activateWorkbenchPluginBackend,
  disableWorkbenchPlugin,
  uninstallWorkbenchPlugin,
  onRefresh,
  mode = 'settings',
  allowLocalInstall = true,
  canMutate = true,
}: PluginManagerProps) {
  const plugins = runtimeIndex?.plugins ?? [];
  const lifecycleEnabled = mode === 'settings' && canMutate;
  const [
    { localPluginId, localSourceValue, pendingTrustPluginId, acceptedCapabilities, busyKey, status, error },
    dispatch,
  ] = useReducer(reducer, initialState);

  // The Errors dock's "Open in Settings → Plugins" CTA (openPluginSettingsFromError)
  // arrives with ?plugin=<id> so the failed row is findable without
  // scrolling/searching. Read once at mount; scroll+highlight below.
  const [highlightPluginId] = useState(() => {
    try {
      return new URLSearchParams(window.location.search).get('plugin') ?? '';
    } catch {
      return '';
    }
  });
  const highlightedRowRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (highlightPluginId) highlightedRowRef.current?.scrollIntoView({ block: 'center' });
  }, [highlightPluginId, plugins.length]);

  const runOperation = async (key: string, task: () => Promise<void>) => {
    dispatch({ type: 'start', key });
    try {
      await task();
      await onRefresh();
      dispatch({ type: 'success', status: key });
    } catch (caught) {
      dispatch({ type: 'error', message: errorMessage(caught) });
    }
  };

  const installLocal = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const pluginId = localPluginId.trim();
    const sourceValue = localSourceValue.trim();
    if (!pluginId || !sourceValue) {
      dispatch({ type: 'error', message: 'Plugin id and local source path are required.' });
      return;
    }
    await runOperation(`install:${pluginId}`, async () => {
      await installLocalWorkbenchPlugin({
        pluginId,
        source: { kind: 'localPath', value: sourceValue },
        arbitraryPackageLoadAllowed: false,
      });
    });
  };

  const reloadPlugin = (plugin: WorkbenchPluginRuntimePlugin) =>
    runOperation(`reload:${plugin.pluginId}`, async () => {
      const sourceValue = pluginSourceValue(plugin);
      if (!sourceValue) {
        throw new Error('Installed plugin source is missing.');
      }
      await installLocalWorkbenchPlugin({
        pluginId: plugin.pluginId,
        source: { kind: 'localPath', value: sourceValue },
        arbitraryPackageLoadAllowed: false,
      });
    });

  const confirmEnablePlugin = (plugin: WorkbenchPluginRuntimePlugin) =>
    runOperation(`enable:${plugin.pluginId}`, async () => {
      if (!plugin.receiptId) {
        throw new Error('Plugin load receipt is required.');
      }
      const requiredCapabilities = plugin.requires?.capabilities ?? [];
      const missingCapability = requiredCapabilities.find(
        (capability) => acceptedCapabilities[capability] !== true,
      );
      if (missingCapability) {
        throw new Error('All plugin capabilities must be accepted before enabling.');
      }
      await activateWorkbenchPlugin({
        pluginId: plugin.pluginId,
        receiptId: plugin.receiptId,
        trustAcknowledged: true,
        permissionsAccepted: requiredCapabilities,
        arbitraryPackageLoadAllowed: false,
      });
    });

  const activateBackend = (plugin: WorkbenchPluginRuntimePlugin) =>
    runOperation(`backend:${plugin.pluginId}`, async () => {
      await activateWorkbenchPluginBackend({
        pluginId: plugin.pluginId,
        trustAcknowledged: true,
        arbitraryPackageLoadAllowed: false,
        executableHandlersAllowed: true,
      });
    });

  const disablePlugin = (plugin: WorkbenchPluginRuntimePlugin) =>
    runOperation(`disable:${plugin.pluginId}`, async () => {
      await disableWorkbenchPlugin(plugin.pluginId);
    });

  const uninstallPlugin = (plugin: WorkbenchPluginRuntimePlugin) =>
    runOperation(`uninstall:${plugin.pluginId}`, async () => {
      await uninstallWorkbenchPlugin(plugin.pluginId);
    });

  return (
    <section
      className="plugin-manager"
      data-testid="plugin-manager"
      data-schema-version={runtimeIndex?.schemaVersion}
      data-project-id={runtimeIndex?.projectId}
      data-plugin-count={plugins.length}
      data-mode={mode}
    >
      {mode === 'settings' && !canMutate && (
        <div className="settings-empty-state" data-testid="plugin-manager-readonly">
          Plugin lifecycle controls require project owner access.
        </div>
      )}
      {lifecycleEnabled && allowLocalInstall && (
        <section
          className="plugin-manager-install-panel"
          aria-labelledby="plugin-manager-install-heading"
        >
          <div className="plugin-manager-section-heading">
            <div>
              <h3 id="plugin-manager-install-heading">Install a local plugin</h3>
              <p>
                Add a plugin that already exists on the computer running Frisket. Installing adds
                the package for the whole workspace; each project chooses whether to enable it.
              </p>
            </div>
          </div>
          <form className="plugin-manager-install" onSubmit={installLocal}>
            <label className="plugin-manager-field">
              <span>Plugin folder or manifest path</span>
              <input
                aria-label="Plugin folder or manifest path"
                aria-describedby="plugin-manager-source-help"
                placeholder="/absolute/path/to/plugin"
                value={localSourceValue}
                onChange={(event) =>
                  dispatch({ type: 'setSourceValue', value: event.currentTarget.value })
                }
                data-testid="plugin-manager-install-source"
              />
              <small id="plugin-manager-source-help">
                Use an absolute server-local path to the plugin folder or its plugin.json file.
                Browser uploads are not supported.
              </small>
            </label>
            <label className="plugin-manager-field">
              <span>Manifest plugin ID</span>
              <input
                aria-label="Manifest plugin ID"
                aria-describedby="plugin-manager-id-help"
                placeholder="example.plugin"
                value={localPluginId}
                onChange={(event) =>
                  dispatch({ type: 'setPluginId', value: event.currentTarget.value })
                }
                data-testid="plugin-manager-install-plugin-id"
              />
              <small id="plugin-manager-id-help">
                Copy the exact id from plugin.json. Frisket uses it to verify the expected plugin.
              </small>
            </label>
            <button
              className="plugin-manager-primary"
              type="submit"
              disabled={busyKey !== null}
              title="Read and validate the local manifest before adding the plugin to this workspace."
              data-testid="plugin-manager-install-local"
            >
              Review and install
            </button>
          </form>
        </section>
      )}
      {lifecycleEnabled && !allowLocalInstall && (
        <div className="settings-empty-state" data-testid="plugin-manager-local-install-disabled">
          Local plugin install is unavailable in hosted workspaces because the service cannot read
          paths or uploads from your computer.
        </div>
      )}
      {status && (
        <output className="plugin-manager-status" data-testid="plugin-manager-status">
          {status}
        </output>
      )}
      {error && (
        <div className="plugin-manager-error" role="alert" data-testid="plugin-manager-error">
          {error}
        </div>
      )}
      <section className="plugin-manager-list" aria-labelledby="plugin-manager-list-heading">
        <div className="plugin-manager-section-heading">
          <div>
            <h3 id="plugin-manager-list-heading">Workspace plugins</h3>
            <p>
              Installed once for this workspace, then enabled per project. See what each plugin
              contributes and what access it requests.
            </p>
          </div>
          <span className="plugin-manager-count">
            {plugins.length} {plugins.length === 1 ? 'plugin' : 'plugins'}
          </span>
        </div>
        {plugins.length === 0 && (
          <div className="settings-empty-state" data-testid="plugin-manager-empty">
            No plugins are installed for this workspace.
          </div>
        )}
        {plugins.map((plugin) => (
          <article
            key={plugin.pluginId}
            ref={plugin.pluginId === highlightPluginId ? highlightedRowRef : undefined}
            className={
              plugin.pluginId === highlightPluginId
                ? 'plugin-manager-installed-plugin plugin-manager-installed-plugin-highlighted'
                : 'plugin-manager-installed-plugin'
            }
            data-testid="plugin-manager-installed-plugin"
            data-plugin-id={plugin.pluginId}
            data-highlighted={plugin.pluginId === highlightPluginId ? 'true' : undefined}
            data-install-state={plugin.installState}
            data-activation={plugin.activation}
            data-registry-activated={String(plugin.registryActivated === true)}
            data-workbench-views={idsForKind(plugin.contributionSummary, 'workbench_view')}
            data-workbench-panels={idsForKind(plugin.contributionSummary, 'workbench_panel')}
            data-actions={idsForKind(plugin.contributionSummary, 'action')}
            data-importers={idsForKind(plugin.contributionSummary, 'importer')}
            data-operators={idsForKind(plugin.contributionSummary, 'operator')}
            data-projections={idsForKind(plugin.contributionSummary, 'projection')}
            data-column-types={idsForKind(plugin.contributionSummary, 'column_type')}
            data-job-handlers={idsForKind(plugin.contributionSummary, 'job_handler')}
            data-contributions={contributionSummaryData(plugin.contributionSummary)}
            data-frontend-bindings={formatFrontendComponentBindings(plugin)}
            data-capabilities={pluginCapabilities(plugin)}
            data-secrets={pluginSecrets(plugin)}
            data-manifest-sha256={plugin.manifestSha256}
            data-package-sha256={plugin.packageSha256 ?? ''}
            data-receipt-id={plugin.receiptId ?? ''}
          >
            <header className="plugin-manager-plugin-heading">
              <div>
                <h4>{plugin.pluginId}</h4>
                <p>Version {plugin.version || 'unknown'}</p>
              </div>
              <PluginStateBadge plugin={plugin} />
            </header>

            {pluginFailureDetail(plugin) && (
              <div
                className="plugin-manager-plugin-failure"
                role={plugin.installState === 'failed' ? 'alert' : undefined}
                data-testid="plugin-manager-plugin-failure"
              >
                <strong>{plugin.installFailure?.code ?? 'Plugin unavailable'}</strong>
                <span>{pluginFailureDetail(plugin)}</span>
              </div>
            )}

            <div className="plugin-manager-facts">
              <section>
                <h5>Contributions</h5>
                {plugin.contributionSummary.length > 0 ? (
                  <ul className="plugin-manager-fact-list" data-testid="plugin-manager-contributions">
                    {plugin.contributionSummary.map((item) => (
                      <li key={item.kind}>
                        <strong>{contributionLabel(item.kind)}</strong>
                        <span>{item.ids.length > 0 ? item.ids.join(', ') : `${item.count} declared`}</span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="plugin-manager-none">No contributions declared.</p>
                )}
              </section>
              <section>
                <h5>Access requested</h5>
                <dl className="plugin-manager-access-list">
                  <div>
                    <dt>Capabilities</dt>
                    <dd data-testid="plugin-manager-readable-capabilities">
                      {(plugin.requires?.capabilities ?? []).length > 0
                        ? (plugin.requires?.capabilities ?? []).join(', ')
                        : 'None'}
                    </dd>
                  </div>
                  <div>
                    <dt>Secret names</dt>
                    <dd data-testid="plugin-manager-readable-secrets">
                      {(plugin.requires?.secrets ?? []).length > 0
                        ? (plugin.requires?.secrets ?? []).join(', ')
                        : 'None'}
                    </dd>
                  </div>
                </dl>
                <p className="plugin-manager-none">Secret values are never shown here.</p>
              </section>
            </div>

            <details className="plugin-manager-technical-details">
              <summary>Workspace package identity</summary>
              <p className="plugin-manager-none">
                Read-only. The source and checksums below identify the package installed for the
                whole workspace; activation is this project's runtime state.
              </p>
              <dl>
                <div>
                  <dt>Source</dt>
                  <dd><code>{pluginSourceValue(plugin) || 'Unavailable'}</code></dd>
                </div>
                <div>
                  <dt>Manifest SHA-256</dt>
                  <dd><code>{plugin.manifestSha256 || 'Unavailable'}</code></dd>
                </div>
                <div>
                  <dt>Package SHA-256</dt>
                  <dd><code>{plugin.packageSha256 || 'Unavailable'}</code></dd>
                </div>
                <div>
                  <dt>Activation (this project)</dt>
                  <dd>{plugin.activation}</dd>
                </div>
              </dl>
            </details>

            {lifecycleEnabled && (
              <section className="plugin-manager-actions" aria-label={`Manage ${plugin.pluginId}`}>
                <div className="plugin-manager-action-buttons">
                  {allowLocalInstall && (
                    <button
                      type="button"
                      disabled={busyKey !== null || !pluginSourceValue(plugin)}
                      onClick={() => void reloadPlugin(plugin)}
                      title="Re-read this plugin's manifest and package from its local path, updating the workspace package."
                      data-testid="plugin-manager-reload"
                    >
                      Reload files
                    </button>
                  )}
                  <button
                    type="button"
                    disabled={busyKey !== null || !plugin.receiptId || plugin.installState === 'enabled'}
                    onClick={() => dispatch({ type: 'beginTrust', pluginId: plugin.pluginId })}
                    title={plugin.installState === 'enabled'
                      ? 'Already enabled'
                      : 'Review requested access, then make this plugin available to the project.'}
                    data-testid="plugin-manager-enable"
                  >
                    Review and enable
                  </button>
                  <button
                    type="button"
                    disabled={busyKey !== null || plugin.installState !== 'enabled'}
                    onClick={() => void activateBackend(plugin)}
                    title="Start the trusted local subprocess that registers executable recipes and job handlers."
                    data-testid="plugin-manager-activate-backend"
                  >
                    Start backend
                  </button>
                  <button
                    type="button"
                    disabled={busyKey !== null || plugin.installState !== 'enabled'}
                    onClick={() => void disablePlugin(plugin)}
                    title="Stop this plugin's contributions without removing its installation record."
                    data-testid="plugin-manager-disable"
                  >
                    Disable
                  </button>
                  <button
                    className="plugin-manager-danger"
                    type="button"
                    disabled={busyKey !== null || plugin.installState === 'uninstalled'}
                    onClick={() => void uninstallPlugin(plugin)}
                    title="Remove this plugin's package from the whole workspace. To turn it off for just this project, use Disable. Source files on disk are not deleted."
                    data-testid="plugin-manager-uninstall"
                  >
                    Uninstall
                  </button>
                </div>
                <details className="plugin-manager-action-guide">
                  <summary>What do these actions do?</summary>
                  <dl>
                    {allowLocalInstall && (
                      <div><dt>Reload files</dt><dd>Re-read changes from the same local path into the workspace package.</dd></div>
                    )}
                    <div><dt>Review and enable</dt><dd>Approve access and expose contributions.</dd></div>
                    <div><dt>Start backend</dt><dd>Register executable recipes and job handlers.</dd></div>
                    <div><dt>Disable</dt><dd>Pause contributions while keeping the installation.</dd></div>
                    <div><dt>Uninstall</dt><dd>Remove the workspace package for every project, not source files.</dd></div>
                  </dl>
                </details>
              </section>
            )}

            {lifecycleEnabled && pendingTrustPluginId === plugin.pluginId && (
              <div
                className="plugin-manager-trust-prompt"
                data-testid="plugin-manager-trust-prompt"
                data-plugin-id={plugin.pluginId}
                data-capabilities={pluginCapabilities(plugin)}
                data-secrets={pluginSecrets(plugin)}
                data-manifest-sha256={plugin.manifestSha256}
                data-package-sha256={plugin.packageSha256 ?? ''}
              >
                <div className="plugin-manager-trust-copy">
                  <strong>Review access for {plugin.pluginId}</strong>
                  <span>Accept every requested capability before enabling the plugin.</span>
                </div>
                {(plugin.requires?.capabilities ?? []).map((capability) => (
                  <label key={capability}>
                    <input
                      type="checkbox"
                      checked={acceptedCapabilities[capability] === true}
                      onChange={(event) =>
                        dispatch({
                          type: 'toggleCapability',
                          capability,
                          accepted: event.currentTarget.checked,
                        })
                      }
                      data-testid={`plugin-manager-capability-${capability}`}
                    />
                    {capability}
                  </label>
                ))}
                {(plugin.requires?.capabilities ?? []).length === 0 && (
                  <span data-testid="plugin-manager-no-capabilities">No capabilities requested</span>
                )}
                {(plugin.requires?.secrets ?? []).length > 0 && (
                  <span>Secret names: {(plugin.requires?.secrets ?? []).join(', ')}</span>
                )}
                <div className="plugin-manager-trust-actions">
                  <button
                    type="button"
                    disabled={
                      busyKey !== null ||
                      !plugin.receiptId ||
                      (plugin.requires?.capabilities ?? []).some(
                        (capability) => acceptedCapabilities[capability] !== true,
                      )
                    }
                    onClick={() => void confirmEnablePlugin(plugin)}
                    data-testid="plugin-manager-confirm-enable"
                  >
                    Trust and enable
                  </button>
                  <button
                    type="button"
                    disabled={busyKey !== null}
                    onClick={() => dispatch({ type: 'cancelTrust' })}
                    data-testid="plugin-manager-cancel-trust"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </article>
        ))}
      </section>
    </section>
  );
}
