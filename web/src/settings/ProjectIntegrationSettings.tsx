import { Button } from '../components/Button';
import { TextInput } from '../components/FormControls';
import { type FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import { Eye, EyeOff, Save, Trash2 } from 'lucide-react';
import { McpServersSection } from './McpServersSection';
import {
  getRuntimeConfig,
  type RuntimeConfig,
  type ProjectInfo,
  type ProjectSecrets,
  type WorkbenchPluginRuntimeIndex,
  type WorkbenchPluginSettings,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { canOwnProject } from '../api/projectRole';
import { PanelSelect } from '../components/PanelSelect';
import { PanelNotice } from '../components/PanelPrimitives';
import { PluginManager } from '../workbench/PluginManager';
import { useLoader, errMsg } from './settingsSupport';
import { InlineStatus, SettingsTable } from './SettingsPrimitives';
import { useSecretEntryDraft } from './useSecretEntryDraft';

export function ProjectSecretsSettings({ project }: { project?: ProjectInfo | null }) {
  const { projectApi: api } = useWorkspaceStores();
  const secrets = useLoader<ProjectSecrets>(() => api.getProjectSecrets());
  const runtimeConfig = useLoader<RuntimeConfig>(() => getRuntimeConfig());
  const pluginsHidden = runtimeConfig.data?.plugins_available !== true;
  const runtime = useLoader<WorkbenchPluginRuntimeIndex | null>(
    () => (pluginsHidden ? Promise.resolve(null) : api.getWorkbenchPluginRuntimeIndex()),
    String(pluginsHidden),
  );
  // Deep-link prefill: the credential gate's
  // "Add in Settings →" arrives with ?secret=<NAME> so the user never
  // retypes an env-var name from memory; the value field takes focus.
  const [prefill] = useState(() => {
    try {
      return new URLSearchParams(window.location.search).get('secret') ?? '';
    } catch {
      return '';
    }
  });
  const entry = useSecretEntryDraft(api, prefill);
  const { name, value, setName, setValue, notice, error: mutationError, setError: setMutationError, saving } = entry;
  // Visible while typing (seeing what you paste beats blind entry —
  // the stored table only ever shows the last-4 hint anyway); the toggle
  // flips to password-masking for shoulder-surfing situations.
  const [valueVisible, setValueVisible] = useState(true);
  const valueRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (prefill) valueRef.current?.focus();
  }, [prefill]);
  const canMutate = canOwnProject(project);
  const requiredSecrets = useMemo(() => {
    const out = new Set<string>();
    for (const plugin of runtime.data?.plugins ?? []) {
      for (const item of plugin.requires?.secrets ?? []) out.add(item);
    }
    return Array.from(out).sort();
  }, [runtime.data]);
  const configured = new Set((secrets.data?.secrets ?? []).map((item) => item.name));
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!canMutate) return;
    const submitted = entry.begin();
    if (!submitted) return;
    try {
      const updated = await api.setProjectSecret(submitted.name, submitted.value);
      if (entry.complete(submitted)) secrets.setData(updated);
    } catch (caught) {
      entry.fail(submitted, caught);
    }
  };
  return (
    <div className="content-stack" data-testid="project-secrets-settings">
      <InlineStatus error={secrets.error ?? runtime.error ?? mutationError} loading={secrets.loading || runtime.loading} notice={notice} />
      {!canMutate && (
        <PanelNotice>
          Your project role can view secret status but cannot create, replace, or delete secrets.
        </PanelNotice>
      )}
      <form className="form-inline" autoComplete="off" onSubmit={save}>
        <TextInput
          aria-label="Project secret name"
          autoComplete="off"
          placeholder="Name"
          value={name}
          disabled={!canMutate}
          onChange={(event) => setName(event.currentTarget.value)}
        />
        <input
          ref={valueRef}
          aria-label="Project secret value"
          type={valueVisible ? 'text' : 'password'}
          autoComplete="new-password"
          placeholder="Value"
          value={value}
          disabled={!canMutate}
          onChange={(event) => setValue(event.currentTarget.value)}
        />
        <Button

          type="button"
          data-testid="secret-value-visibility"
          aria-label={valueVisible ? 'Hide secret value' : 'Show secret value'}
          aria-pressed={!valueVisible}
          onClick={() => setValueVisible((current) => !current)}
        >
          {valueVisible ? <EyeOff size={14} /> : <Eye size={14} />}
        </Button>
        <Button className="btn-primary" type="submit" aria-busy={saving} disabled={saving || !canMutate || !name.trim() || !value}><Save size={14} /> Save</Button>
      </form>
      <SettingsTable
        headers={['Secret', 'Value', 'Consumers', 'Updated', '']}
        rows={(secrets.data?.secrets ?? []).map((row) => [
          row.name,
          row.hint,
          row.consumers.map((consumer) => consumer.id).join(', ') || '-',
          row.updatedAt ?? '-',
          <button
            key={row.name}
            className="icon-btn"
            type="button"
            aria-label={`Delete ${row.name} secret`}
            title={`Delete ${row.name} secret`}
            disabled={!canMutate}
            onClick={() => void api.deleteProjectSecret(row.name)
              .then(secrets.reload)
              .catch((caught) => setMutationError(errMsg(caught)))}
          >
            <Trash2 size={14} />
          </button>,
        ])}
        empty="No project secrets"
      />
      <SettingsTable
        headers={['Required by plugins', 'Status']}
        rows={requiredSecrets.map((secret) => [secret, configured.has(secret) ? 'Configured' : 'Missing'])}
        empty="No plugin secret requirements"
      />
      <SettingsTable
        headers={['Migration conflict', 'Plugin', 'Status']}
        rows={(secrets.data?.conflicts ?? []).map((conflict) => [
          conflict.name,
          conflict.plugin_id,
          conflict.status,
        ])}
        empty="No migration conflicts"
      />
    </div>
  );
}

export function ProjectMcpServersSettings() {
  const { projectApi: api } = useWorkspaceStores();
  const servers = useLoader(() => api.listMcpServers());
  const secrets = useLoader<ProjectSecrets>(() => api.getProjectSecrets());
  const reload = () => servers.reload();
  return <McpServersSection
    servers={(servers.data ?? []).map((server) => ({ ...server, last_discovered_tool_count: server.last_discovered_tool_count ?? undefined, last_test: server.lastTest ? { status: server.lastTest.status === 'succeeded' ? 'passed' : 'failed', tested_at: server.lastTest.tested_at, detail: server.lastTest.diagnostic ?? undefined } : null }))}
    projectSecrets={(secrets.data?.secrets ?? []).map((secret) => secret.name)}
    onCreate={(draft) => api.createMcpServer(draft).then(reload)}
    onUpdate={(id, patch) => api.updateMcpServer(id, patch).then(reload)}
    onRemove={(id) => api.deleteMcpServer(id).then(reload)}
    onTest={(id) => api.testMcpServer(id).then((server) => { void reload(); return { status: server.lastTest?.status === 'succeeded' ? 'passed' : 'failed', tested_at: server.lastTest?.tested_at, discovered_tool_count: server.last_discovered_tool_count ?? undefined, detail: server.lastTest?.diagnostic ?? undefined }; })}
  />;
}

function PluginOptionsList({ runtimeIndex }: { runtimeIndex: WorkbenchPluginRuntimeIndex | null }) {
  const { projectApi: api } = useWorkspaceStores();
  const [settingsByPlugin, setSettingsByPlugin] = useState<Record<string, WorkbenchPluginSettings>>({});
  const [drafts, setDrafts] = useState<Record<string, Record<string, unknown>>>({});
  const [error, setError] = useState<string | null>(null);
  const plugins = useMemo(() => runtimeIndex?.plugins ?? [], [runtimeIndex]);
  useEffect(() => {
    let alive = true;
    if (plugins.length === 0) {
      return () => {
        alive = false;
      };
    }
    void Promise.all(
      plugins.map(async (plugin) => {
        try {
          return {
            payload: await api.getWorkbenchPluginSettings(plugin.pluginId),
            error: null,
          };
        } catch (caught) {
          return {
            payload: null,
            error: `${plugin.pluginId}: ${errMsg(caught)}`,
          };
        }
      }),
    ).then((results) => {
      if (!alive) return;
      const next: Record<string, WorkbenchPluginSettings> = {};
      for (const result of results) {
        if (result.payload) next[result.payload.pluginId] = result.payload;
      }
      setSettingsByPlugin(next);
      const failures = results
        .map((result) => result.error)
        .filter((message): message is string => Boolean(message));
      setError(failures.length ? `Could not load plugin settings: ${failures.join('; ')}` : null);
    });
    return () => {
      alive = false;
    };
  }, [plugins]);
  const rows = plugins.flatMap((plugin) => {
    const payload = settingsByPlugin[plugin.pluginId];
    return payload
      ? payload.settings.map((setting) => ({ payload, setting }))
      : [];
  });
  if (rows.length === 0) {
    return (
      <div className="content-stack" data-testid="plugin-options-list">
        <InlineStatus error={plugins.length ? error : null} />
        <div className="panel-empty" data-testid="plugin-options-empty">
          No configurable plugin options.
        </div>
      </div>
    );
  }
  return (
    <div className="content-stack" data-testid="plugin-options-list">
      <InlineStatus error={plugins.length ? error : null} />
      {rows.map(({ payload, setting }) => {
        const draft = drafts[payload.pluginId]?.[setting.id] ?? setting.effectiveValue ?? '';
        return (
          <form
            key={`${payload.pluginId}:${setting.id}`}
            className="form-inline"
            onSubmit={(event) => {
              event.preventDefault();
              if (!payload.canMutate || setting.readOnly) return;
              void api.patchWorkbenchPluginSettings(payload.pluginId, { [setting.id]: draft })
                .then((updated) => setSettingsByPlugin((state) => ({ ...state, [payload.pluginId]: updated })))
                .catch((caught) => setError(errMsg(caught)));
            }}
          >
            <span className="settings-option-label">{setting.title}</span>
            {setting.type === 'boolean' ? (
              <input
                aria-label={setting.title}
                data-testid={`plugin-option-${setting.id}`}
                type="checkbox"
                checked={Boolean(draft)}
                disabled={!payload.canMutate || setting.readOnly}
                onChange={(event) => {
                  const checked = event.currentTarget.checked;
                  setDrafts((state) => ({
                    ...state,
                    [payload.pluginId]: {
                      ...(state[payload.pluginId] ?? {}),
                      [setting.id]: checked,
                    },
                  }));
                }}
              />
            ) : setting.type === 'enum' ? (
              <PanelSelect
                aria-label={setting.title}
                data-testid={`plugin-option-${setting.id}`}
                value={String(draft)}
                disabled={!payload.canMutate || setting.readOnly}
                onChange={(event) => {
                  const value = event.currentTarget.value;
                  setDrafts((state) => ({
                    ...state,
                    [payload.pluginId]: {
                      ...(state[payload.pluginId] ?? {}),
                      [setting.id]: value,
                    },
                  }));
                }}
              >
                {(setting.enum ?? []).map((item) => <option key={String(item)} value={String(item)}>{String(item)}</option>)}
              </PanelSelect>
            ) : (
              <TextInput
                aria-label={setting.title}
                data-testid={`plugin-option-${setting.id}`}
                value={String(draft)}
                disabled={!payload.canMutate || setting.readOnly}
                onChange={(event) => {
                  const value = event.currentTarget.value;
                  setDrafts((state) => ({
                    ...state,
                    [payload.pluginId]: {
                      ...(state[payload.pluginId] ?? {}),
                      [setting.id]: setting.type === 'number'
                        ? Number(value)
                        : value,
                    },
                  }));
                }}
              />
            )}
            <Button

              data-testid={`plugin-option-save-${setting.id}`}
              type="submit"
              disabled={!payload.canMutate || setting.readOnly}
            >
              Save
            </Button>
          </form>
        );
      })}
    </div>
  );
}

export function ProjectPluginsSettings({
  identityMode,
  project,
}: {
  identityMode: boolean;
  project?: ProjectInfo | null;
}) {
  const { projectApi: api } = useWorkspaceStores();
  // Unknown and failed config loads remain closed. Bundled-only compositions
  // still load plugin contributions elsewhere, but do not expose lifecycle UI.
  const runtimeConfig = useLoader<RuntimeConfig>(() => getRuntimeConfig());
  const hidden = runtimeConfig.data?.plugin_management_available !== true;
  const runtime = useLoader<WorkbenchPluginRuntimeIndex | null>(
    () => (hidden ? Promise.resolve(null) : api.getWorkbenchPluginRuntimeIndex()),
    String(hidden),
  );
  const canMutate = canOwnProject(project);
  if (hidden) {
    return (
      <div className="content-stack" data-testid="project-plugins-settings">
        <PanelNotice>
          Plugin management is not available on this server.
        </PanelNotice>
      </div>
    );
  }
  return (
    <div className="content-stack" data-testid="project-plugins-settings">
      <InlineStatus error={runtime.error} loading={runtime.loading} />
      {!canMutate && (
        <PanelNotice>
          Your project role can view plugin status but cannot change plugin lifecycle or settings.
        </PanelNotice>
      )}
      <PluginManager
        runtimeIndex={runtime.data}
        installLocalWorkbenchPlugin={api.installLocalWorkbenchPlugin.bind(api)}
        activateWorkbenchPlugin={api.activateWorkbenchPlugin.bind(api)}
        activateWorkbenchPluginBackend={api.activateWorkbenchPluginBackend.bind(api)}
        disableWorkbenchPlugin={api.disableWorkbenchPlugin.bind(api)}
        uninstallWorkbenchPlugin={api.uninstallWorkbenchPlugin.bind(api)}
        onRefresh={runtime.reload}
        mode="settings"
        allowLocalInstall={!identityMode}
        canMutate={canMutate}
      />
      <PluginOptionsList runtimeIndex={runtime.data} />
    </div>
  );
}
