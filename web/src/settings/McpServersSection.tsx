import { useMemo, useState } from 'react';

export type McpServerEnvironmentValue =
  | { project_secret: string }
  | { value: string };

export interface McpServerSettingsEntry {
  id: string;
  name: string;
  command: string;
  args: string[];
  cwd?: string | null;
  env: Record<string, McpServerEnvironmentValue>;
  enabled: boolean;
  last_discovered_tool_count?: number;
  last_test?: {
    status: 'passed' | 'failed';
    tested_at?: string | null;
    detail?: string;
  } | null;
}

export interface McpServerDraft {
  name: string;
  command: string;
  args: string[];
  cwd: string | null;
  env: Record<string, McpServerEnvironmentValue>;
  enabled: boolean;
}

export interface McpServerTestResult {
  status: 'passed' | 'failed';
  tested_at?: string | null;
  discovered_tool_count?: number;
  detail?: string;
}

export interface McpServersSectionProps {
  servers: readonly McpServerSettingsEntry[];
  projectSecrets: readonly string[];
  onCreate: (server: McpServerDraft) => void | Promise<void>;
  onUpdate: (id: string, patch: Partial<McpServerDraft>) => void | Promise<void>;
  onRemove: (id: string) => void | Promise<void>;
  onTest: (id: string) => Promise<McpServerTestResult>;
}

type EnvironmentDraft = { name: string; projectSecret: string; literalValue: string; kind: 'project_secret' | 'value' };

type FormState = {
  id: string | null;
  name: string;
  command: string;
  argumentsText: string;
  cwd: string;
  env: EnvironmentDraft[];
  enabled: boolean;
};

const EMPTY_FORM: FormState = {
  id: null,
  name: '',
  command: '',
  argumentsText: '',
  cwd: '',
  env: [],
  enabled: true,
};

function titleCase(name: string): string {
  return name
    .toLowerCase()
    .split('_')
    .filter(Boolean)
    .map((part) => `${part.slice(0, 1).toUpperCase()}${part.slice(1)}`)
    .join(' ');
}

function formForServer(server: McpServerSettingsEntry): FormState {
  return {
    id: server.id,
    name: server.name,
    command: server.command,
    argumentsText: JSON.stringify(server.args),
    cwd: server.cwd ?? '',
    enabled: server.enabled,
    env: Object.entries(server.env).map(([name, value]) => ({
      name,
      projectSecret: 'project_secret' in value ? value.project_secret : '',
      literalValue: 'value' in value ? value.value : '',
      kind: 'project_secret' in value ? 'project_secret' : 'value',
    })),
  };
}

function relativeTestTime(testedAt: string): string {
  const elapsed = Math.max(0, Date.now() - new Date(testedAt).getTime());
  const minutes = Math.floor(elapsed / 60_000);
  if (minutes < 1) return 'just now';
  return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
}

function serverStatus(server: McpServerSettingsEntry): string {
  const availability = server.enabled ? 'Enabled' : 'Disabled';
  if (!server.last_test) return `${availability} · never tested`;
  if (server.last_test.status === 'failed') return `${availability} · last test failed`;
  if (!server.last_test.tested_at) return `${availability} · last test passed`;
  return `${availability} · last tested ${relativeTestTime(server.last_test.tested_at)}`;
}

function importedForm(rawJson: string): FormState | null {
  try {
    const parsed: unknown = JSON.parse(rawJson);
    if (!parsed || typeof parsed !== 'object') return null;
    const mcpServers = (parsed as { mcpServers?: unknown }).mcpServers;
    if (!mcpServers || typeof mcpServers !== 'object') return null;
    const first = Object.entries(mcpServers as Record<string, unknown>)[0];
    if (!first) return null;
    const [name, entry] = first;
    if (!entry || typeof entry !== 'object') return null;
    const value = entry as { command?: unknown; args?: unknown; cwd?: unknown; env?: unknown };
    if (typeof value.command !== 'string') return null;
    const args = Array.isArray(value.args)
      ? value.args.filter((arg): arg is string => typeof arg === 'string')
      : [];
    const env = value.env && typeof value.env === 'object'
      ? Object.keys(value.env as Record<string, unknown>).map((key) => ({ name: key, projectSecret: '', literalValue: '', kind: 'project_secret' as const }))
      : [];
    return {
      id: null,
      name,
      command: value.command,
      argumentsText: JSON.stringify(args),
      cwd: typeof value.cwd === 'string' ? value.cwd : '',
      env,
      enabled: true,
    };
  } catch {
    return null;
  }
}

function toDraft(form: FormState): McpServerDraft {
  let args: string[] = [];
  if (form.argumentsText.trim()) {
    try {
      const parsed: unknown = JSON.parse(form.argumentsText);
      args = Array.isArray(parsed) && parsed.every((item) => typeof item === 'string') ? parsed : [form.argumentsText.trim()];
    } catch {
      args = [form.argumentsText.trim()];
    }
  }
  return {
    name: form.name.trim(),
    command: form.command.trim(),
    args,
    cwd: form.cwd.trim() || null,
    env: Object.fromEntries(
      form.env
        .filter((entry) => entry.name.trim() && (entry.kind === 'value' || entry.projectSecret))
        .map((entry) => [entry.name.trim(), entry.kind === 'value' ? { value: entry.literalValue } : { project_secret: entry.projectSecret }]),
    ),
    enabled: form.enabled,
  };
}

function hasUnmappedEnvironment(form: FormState): boolean {
  return form.env.some((entry) => entry.name.trim() && entry.kind === 'project_secret' && !entry.projectSecret);
}

/**
 * Pure controlled settings surface. Persistence, permission checks, and the
 * subprocess test implementation live at its caller/API boundary.
 */
export function McpServersSection({
  servers,
  projectSecrets,
  onCreate,
  onUpdate,
  onRemove,
  onTest,
}: McpServersSectionProps) {
  const [form, setForm] = useState<FormState | null>(null);
  const [importJson, setImportJson] = useState<string | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, McpServerTestResult>>({});

  const editing = form?.id ? servers.find((server) => server.id === form.id) ?? null : null;
  const unmappedEnvironment = form ? hasUnmappedEnvironment(form) : false;
  const canSave = Boolean(form?.name.trim() && form.command.trim() && !unmappedEnvironment);
  const visibleServers = useMemo(() => [...servers], [servers]);

  const updateForm = (patch: Partial<FormState>) => setForm((current) => current ? { ...current, ...patch } : current);
  const updateEnv = (index: number, patch: Partial<EnvironmentDraft>) => setForm((current) => {
    if (!current) return current;
    return {
      ...current,
      env: current.env.map((entry, entryIndex) => entryIndex === index ? { ...entry, ...patch } : entry),
    };
  });

  const save = async () => {
    if (!form || !canSave) return;
    const draft = toDraft(form);
    if (form.id) await onUpdate(form.id, draft);
    else await onCreate(draft);
    setForm(null);
    setImportJson(null);
  };

  const startImport = () => {
    try {
      const decoded = JSON.parse(importJson ?? '') as { mcpServers?: unknown };
      if (decoded.mcpServers && typeof decoded.mcpServers === 'object' && Object.keys(decoded.mcpServers).length > 1) {
        setImportError('Import one MCP server at a time so every environment value can be classified explicitly.');
        return;
      }
    } catch {
      // The shared parser below owns the ordinary invalid-JSON message.
    }
    const parsed = importedForm(importJson ?? '');
    if (!parsed) {
      setImportError('Enter a valid standard mcpServers JSON object.');
      return;
    }
    setImportError(null);
    setImportJson(null);
    setForm(parsed);
  };

  return (
    <section className="settings-stack" data-testid="project-mcp-servers-settings">
      <h2>MCP Servers</h2>
      <p className="settings-copy">Configure trusted local stdio MCP servers for Tool-assisted Extract.</p>

      {!form && importJson === null && (
        <div className="settings-row-actions">
          <button type="button" onClick={() => setForm(EMPTY_FORM)}>Add MCP server</button>
          <button type="button" onClick={() => setImportJson('')}>Import MCP JSON</button>
        </div>
      )}

      {importJson !== null && (
        <div className="settings-form">
          <label>
            MCP server JSON
            <textarea aria-label="MCP server JSON" value={importJson} onChange={(event) => setImportJson(event.currentTarget.value)} />
          </label>
          {importError && <p role="alert">{importError}</p>}
          <div className="settings-row-actions">
            <button type="button" onClick={startImport}>Import</button>
            <button type="button" onClick={() => { setImportJson(null); setImportError(null); }}>Cancel</button>
          </div>
        </div>
      )}

      {form && (
        <div className="settings-form" data-testid="mcp-server-form">
          {form.env.length > 0 && <p>Map imported environment values to Project Secrets before saving.</p>}
          <label>
            Name
            <input aria-label="Name" value={form.name} onChange={(event) => updateForm({ name: event.currentTarget.value })} />
          </label>
          <label>
            Command
            <input aria-label="Command" value={form.command} onChange={(event) => updateForm({ command: event.currentTarget.value })} />
          </label>
          <label>
            Arguments
            <input aria-label="Arguments" value={form.argumentsText} onChange={(event) => updateForm({ argumentsText: event.currentTarget.value })} />
          </label>
          <label>
            Working directory
            <input aria-label="Working directory" value={form.cwd} onChange={(event) => updateForm({ cwd: event.currentTarget.value })} />
          </label>
          {form.env.map((entry, index) => (
            <div key={index} className="settings-inline-form">
              <label>
                Environment variable name
                <input
                  aria-label={entry.name ? `${titleCase(entry.name)} environment variable name` : 'Environment variable name'}
                  value={entry.name}
                  onChange={(event) => updateEnv(index, { name: event.currentTarget.value })}
                />
              </label>
              <div>
                <span>{entry.name ? titleCase(entry.name) : 'Project Secret'}</span>
                <select aria-label={`${entry.name ? titleCase(entry.name) : 'Environment'} value kind`} value={entry.kind} onChange={(event) => updateEnv(index, { kind: event.currentTarget.value as EnvironmentDraft['kind'] })}>
                  <option value="project_secret">Project Secret</option><option value="value">Literal value</option>
                </select>
                {entry.kind === 'value' ? <input aria-label={entry.name ? titleCase(entry.name) : 'Literal value'} value={entry.literalValue} onChange={(event) => updateEnv(index, { literalValue: event.currentTarget.value })} /> : (
                <select
                  aria-label={entry.name ? titleCase(entry.name) : 'Project Secret'}
                  value={entry.projectSecret}
                  onChange={(event) => updateEnv(index, { projectSecret: event.currentTarget.value })}
                >
                  <option value="">Select a Project Secret</option>
                  {projectSecrets.map((secret) => <option key={secret} value={secret}>{secret}</option>)}
                </select>
                )}
              </div>
              <button type="button" aria-label={`Remove ${entry.name || 'environment variable'}`} onClick={() => updateForm({ env: form.env.filter((_, entryIndex) => entryIndex !== index) })}>Remove</button>
            </div>
          ))}
          <button type="button" onClick={() => updateForm({ env: [...form.env, { name: '', projectSecret: '', literalValue: '', kind: 'project_secret' }] })}>Add environment variable</button>
          <div className="settings-row-actions">
            <button type="button" disabled={!canSave} onClick={() => void save()}>Save</button>
            <button type="button" onClick={() => { setForm(null); setImportJson(null); }}>Cancel</button>
          </div>
        </div>
      )}

      <div className="settings-table-wrap">
        {visibleServers.map((server) => {
          const latestTest = testResults[server.id];
          return (
            <article key={server.id} data-testid={`mcp-server-${server.id}`} className="settings-row">
              <strong>{server.name}</strong>
              <div>{serverStatus(server)}</div>
              {server.last_discovered_tool_count !== undefined && <div>{server.last_discovered_tool_count} tools last discovered</div>}
              {server.last_test?.detail && <div>{server.last_test.detail}</div>}
              {latestTest && <div data-testid={`mcp-server-test-result-${server.id}`}>{latestTest.status}</div>}
              <div className="settings-row-actions">
                <button type="button" aria-label={`Edit ${server.name}`} onClick={() => setForm(formForServer(server))}>Edit</button>
                <button
                  type="button"
                  aria-label={`Test ${server.name}`}
                  onClick={() => void onTest(server.id).then((result) => setTestResults((current) => ({ ...current, [server.id]: result })))}
                >
                  Test
                </button>
                <button type="button" aria-label={`${server.enabled ? 'Disable' : 'Enable'} ${server.name}`} onClick={() => void onUpdate(server.id, { enabled: !server.enabled })}>
                  {server.enabled ? 'Disable' : 'Enable'}
                </button>
                <button type="button" aria-label={`Remove ${server.name}`} onClick={() => void onRemove(server.id)}>Remove</button>
              </div>
            </article>
          );
        })}
      </div>
      {editing && <span className="sr-only">Editing {editing.name}</span>}
    </section>
  );
}
