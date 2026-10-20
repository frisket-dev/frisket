import { Button } from '../components/Button';
import { FormField, TextInput } from '../components/FormControls';
import { type FormEvent, type ReactNode, useState } from 'react';
import { KeyRound, RefreshCw, Save, Trash2 } from 'lucide-react';
import { ProviderKeyForm } from '../engine-selector/ProviderKeyForm';
import { ModelsGatewaySettings } from '../engine-selector/ModelsGatewaySettings';
import { LocalServerGuidance } from '../components/LocalServerGuidance';
import { type LocalEndpointPatch } from '../api/localProviders';
import {
  createLocalEndpoint,
  discoverLocalEndpoints,
  deleteLocalEndpoint,
  deleteProviderKey,
  listModelPulls,
  listProviders,
  providerStatus,
  setProviderKey,
  validateProviderKey,
  updateLocalEndpoint,
  type LocalProviderCatalog,
  type LocalProviderEntry,
  type LocalEndpointDiscoveryCandidate,
  type LocalHttpEndpointEntry,
  type ProjectInfo,
  type ProjectProviderKeyInfo,
  type ProjectProviderKeys,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { canOwnProject } from '../api/projectRole';
import { ModelPullHistory } from './ModelPullHistory';
import { PanelNotice, StatusChip } from '../components/PanelPrimitives';
import { formatUsdOrNone } from '../format';
import { projectProviderKeySource } from './projectProviderKeySource';
import { ProjectCloudRoutingSettings, type ProjectCloudRoutingView } from '../cloud/ProjectCloudRoutingSettings';
import { cloudConnectionApi, projectCloudRoutingApi } from '../api/cloudConnection';
import { useLoader, errMsg } from './settingsSupport';
import { useExistingProviderValidation } from './useExistingProviderValidation';
import { InlineStatus, SettingsTable } from './SettingsPrimitives';

/**
 * Accrued spend on one project provider key.
 *
 * `spent_usd` only sums calls we could price. When `unmetered_calls` is
 * non-zero the number is a LOWER BOUND, and the cell says so rather than
 * presenting a partial total as if it were complete — an unpriced call spent
 * real money we cannot name, which is not the same as spending zero.
 *
 * Covered: LLM calls made through a project provider key, plus routed
 * transcribe/OCR work billed to one. NOT covered: spend on org or platform
 * keys, and LLM calls outside a run (ask, preview translation, sandbox
 * broker) — those never reach the accrual seam.
 */
function SpentCell({ row }: { row: ProjectProviderKeyInfo }): ReactNode {
  const unmetered = row.unmetered_calls;
  if (!row.configured) return <>—</>;
  if (unmetered === 0) return <>{formatUsdOrNone(row.spent_usd)}</>;
  return (
    <span title={`${unmetered} call${unmetered === 1 ? '' : 's'} had no published price, so the real total is higher.`}>
      {formatUsdOrNone(row.spent_usd)}+
      <span className="settings-row-note">
        +{unmetered} unpriced call{unmetered === 1 ? '' : 's'}
      </span>
    </span>
  );
}

// Exported for component tests, same precedent as the two AI-providers
// sections above: the money cells (cap, spent, unset) are the surface the
// sub-cent formatter exists for, and asserting them through the full
// SettingsSectionRenderer would test the router instead.
export function ProjectAiProvidersSettings({
  project,
  cloudConnectionAvailable = false,
}: {
  project?: ProjectInfo | null;
  cloudConnectionAvailable?: boolean;
}) {
  const { projectApi: api } = useWorkspaceStores();
  const keys = useLoader<ProjectProviderKeys>(() => api.getProjectProviderKeys());
  const [adding, setAdding] = useState(false);
  const [cloudRouting, setCloudRouting] = useState<ProjectCloudRoutingView | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const canMutate = canOwnProject(project);
  const { existingTests, testExisting } = useExistingProviderValidation(
    api, (provider, signal) => api.validateProjectProviderKey(provider, undefined, { signal }), canMutate,
  );
  return (
    <div className="content-stack" data-testid="project-ai-providers-settings">
      {cloudConnectionAvailable && project?.id && <ProjectCloudRoutingSettings
        projectId={project.id} connectionClient={cloudConnectionApi}
        routingClient={projectCloudRoutingApi} onRoutingChange={setCloudRouting} />}
      <InlineStatus error={keys.error ?? mutationError} loading={keys.loading} notice={notice} />
      {!canMutate && (
        <PanelNotice>
          Your project role can view provider override status but cannot change provider keys.
        </PanelNotice>
      )}
      <Button type="button" disabled={!canMutate} onClick={() => { setNotice(null); setMutationError(null); setAdding((current) => !current); }}>
        <KeyRound size={14} /> Add new key
      </Button>
      {adding && <ProviderKeyForm key={`${project?.id}:${canMutate}`} canMutate={canMutate} keyLabel="Project provider key" spendCap providers={keys.data?.providers ?? []}
        validate={(provider, key, signal) => api.validateProjectProviderKey(provider, key, { signal })}
        save={(provider, key, receipt, cap, signal) => api.setProjectProviderKey(provider, key, cap, receipt, { signal })}
        onSaved={(saved) => { keys.setData(saved); setAdding(false); setNotice('Provider override saved'); }} />}
      <SettingsTable
        headers={['Provider', 'Secret', 'Key', 'Cap', 'Spent', '']}
        rows={(keys.data?.providers ?? []).map((row) => {
          const keySource = projectProviderKeySource(row, cloudRouting);
          return [
          row.label,
          row.secret_name,
          <>
            {keySource.kind === 'cloud'
              ? <a href={keySource.settingsUrl} target="_blank" rel="noreferrer">{keySource.label}</a>
              : keySource.kind === 'missing'
                ? <button className="btn-link" type="button" disabled={!canMutate}
                    onClick={() => setAdding(true)}>{keySource.label}</button>
                : <span>{keySource.label}</span>}
            {existingTests[row.id] && (
              <div
                className="panel-notice" data-tone={existingTests[row.id].tone === 'pending' ? 'neutral' : existingTests[row.id].tone}
                data-testid={`provider-existing-validation-${row.id}`}
              >
                {existingTests[row.id].text}
              </div>
            )}
          </>,
          formatUsdOrNone(row.spend_cap_usd),
          <SpentCell key={`${row.id}-spent`} row={row} />,
          row.configured ? (
            <span key={row.id} className="control-row">
              <Button
                className="btn-compact"
                type="button"
                aria-label={`Test ${row.label} provider override`}
                disabled={!canMutate}
                onClick={() => void testExisting(row.id, row.label)}
              >
                Test
              </Button>
              <button
                className="icon-btn"
                type="button"
                aria-label={`Delete ${row.label} provider override`}
                title={`Delete ${row.label} provider override`}
                disabled={!canMutate}
                onClick={() => void api.deleteProjectProviderKey(row.id)
                  .then(keys.reload)
                  .catch((caught) => setMutationError(errMsg(caught)))}
              >
                <Trash2 size={14} />
              </button>
            </span>
          ) : '',
          ];
        })}
        empty="No provider catalog"
      />
    </div>
  );
}

// The workspace-level provider page: deliberately the SAME interface as
// ProjectAiProvidersSettings above — the
// shared ProviderKeyForm controls (validate before save) and a
// SettingsTable of provider rows with Test/Delete — but backed by the
// workspace catalog (/api/providers → <ws>/.frisket/provider_keys.json),
// plus the canonical local endpoint list. Exported for component tests.
export function WorkspaceAiProvidersSettings() {
  const catalog = useLoader<LocalProviderCatalog>(() => listProviders());
  const [adding, setAdding] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const { existingTests, testExisting } = useExistingProviderValidation(
    'workspace', (provider, signal) => validateProviderKey(provider, undefined, { signal }),
  );
  const providers = catalog.data?.providers ?? [];
  const keyProviders = providers.filter((provider) => provider.kind === 'platform_api');
  const localServers = providers.filter(
    (provider): provider is LocalHttpEndpointEntry => provider.kind === 'local_http',
  );
  const recheckLocalServer = async (providerId: string) => {
    const entry = await providerStatus(providerId);
    const current = catalog.data;
    if (!current) return;
    catalog.setData({
      ...current,
      providers: current.providers.map((provider) => (
        (provider.kind === 'local_http' ? provider.endpoint_id : provider.id) ===
        (entry.kind === 'local_http' ? entry.endpoint_id : entry.id)
          ? entry
          : provider
      )),
    });
  };

  const sourceLabel = (row: Extract<LocalProviderEntry, { kind: 'platform_api' }>): string =>
    row.source === 'env' ? 'Environment' : row.source === 'local_file' ? 'Workspace file' : '—';
  return (
    <div className="content-stack" data-testid="workspace-ai-providers-settings">
      <InlineStatus error={catalog.error ?? mutationError} loading={catalog.loading} notice={notice} />
      <p className="form-hint">
        Keys are stored in this workspace only and never sent back to the browser.
        A shell-exported key always wins over one entered here.
      </p>
      <Button type="button" onClick={() => { setNotice(null); setMutationError(null); setAdding((current) => !current); }}>
        <KeyRound size={14} /> Add new key
      </Button>
      {adding && <ProviderKeyForm canMutate keyLabel="Provider key" providers={keyProviders}
        validate={(provider, key, signal) => validateProviderKey(provider, key, { signal })}
        save={(provider, key, receipt, _cap, signal) => setProviderKey(provider, key, receipt, { signal })}
        onSaved={(saved) => { catalog.setData(saved); setAdding(false); setNotice('Provider key saved'); }} />}
      <SettingsTable
        headers={['Provider', 'Source', 'Status', '']}
        rows={keyProviders.map((row) => [
          row.label,
          sourceLabel(row),
          <>
            <span data-testid={`provider-status-${row.id}`}>
              {row.configured ? `Configured ${row.hint ?? ''}` : 'Not configured'}
            </span>
            {existingTests[row.id] && (
              <div
                className="panel-notice" data-tone={existingTests[row.id].tone === 'pending' ? 'neutral' : existingTests[row.id].tone}
                data-testid={`provider-test-result-${row.id}`}
              >
                {existingTests[row.id].text}
              </div>
            )}
          </>,
          row.configured ? (
            <span key={row.id} className="control-row">
              <Button
                className="btn-compact"
                type="button"
                data-testid={`provider-key-test-${row.id}`}
                aria-label={`Test ${row.label} provider key`}
                onClick={() => void testExisting(row.id, row.label)}
              >
                Test
              </Button>
              {row.source !== 'env' && (
                <button
                  className="icon-btn"
                  type="button"
                  data-testid={`provider-key-delete-${row.id}`}
                  aria-label={`Delete ${row.label} provider key`}
                  title={`Delete ${row.label} provider key`}
                  onClick={() => void deleteProviderKey(row.id)
                    .then(catalog.reload)
                    .catch((caught) => setMutationError(errMsg(caught)))}
                >
                  <Trash2 size={14} />
                </button>
              )}
            </span>
          ) : '',
        ])}
        empty="No provider catalog"
      />
      {catalog.data?.tier === 'local' && (
        <LocalEndpointsSettings
          endpoints={localServers}
          onReload={catalog.reload}
          onRecheck={recheckLocalServer}
        />
      )}
      <ModelsGatewaySettings scope="workspace" />
    </div>
  );
}

const LOCAL_ENDPOINT_SUGGESTIONS = [
  { label: 'Ollama', origin: 'http://localhost:11434' },
  { label: 'LM Studio', origin: 'http://localhost:1234' },
] as const;

function localEndpointDiscoverySummary(
  candidates: LocalEndpointDiscoveryCandidate[],
): string {
  return candidates.map((candidate) => {
    if (candidate.outcome === 'added') {
      return `Found ${candidate.label} at ${candidate.origin} — added.`;
    }
    if (candidate.outcome === 'already_added') {
      return `${candidate.label} at ${candidate.origin} — already added.`;
    }
    return `Nothing found at ${candidate.origin} (${candidate.label}).`;
  }).join(' ');
}

function LocalEndpointsSettings({
  endpoints,
  onReload,
  onRecheck,
}: {
  endpoints: LocalHttpEndpointEntry[];
  onReload: () => Promise<void>;
  onRecheck: (providerId: string) => Promise<void>;
}) {
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const configuredOrigins = new Set(endpoints.map((endpoint) => endpoint.origin));
  const suggestions = LOCAL_ENDPOINT_SUGGESTIONS.filter(
    (suggestion) => !configuredOrigins.has(suggestion.origin),
  );

  const add = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || !name.trim() || !url.trim()) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await createLocalEndpoint({ display_name: name.trim(), origin: url.trim() });
      await onReload();
      setName('');
      setUrl('');
      setAdding(false);
    } catch (caught) {
      setError(errMsg(caught));
    } finally {
      setBusy(false);
    }
  };

  const addSuggestion = async (label: string, origin: string) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await createLocalEndpoint({ display_name: label, origin });
      await onReload();
      setNotice(`${label} added.`);
    } catch (caught) {
      setError(errMsg(caught));
    } finally {
      setBusy(false);
    }
  };

  const discover = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await discoverLocalEndpoints();
      await onReload();
      setNotice(localEndpointDiscoverySummary(result.candidates));
    } catch (caught) {
      setError(errMsg(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="content-stack" data-testid="local-endpoints">
      <div className="settings-section-kicker">Local model servers</div>
      <p className="form-hint">
        Connect several OpenAI-compatible servers at once. Each server keeps a
        stable identity, so the same model name on two machines routes to the
        machine you selected.
      </p>
      {endpoints.map((endpoint) => (
        <LocalEndpointRow
          key={endpoint.endpoint_id}
          endpoint={endpoint}
          onReload={onReload}
          onRecheck={() => onRecheck(endpoint.endpoint_id)}
        />
      ))}
      {suggestions.length > 0 && (
        <div className="control-row" data-testid="local-endpoint-suggestions">
          {suggestions.map((suggestion) => (
            <Button

              type="button"
              key={suggestion.origin}
              disabled={busy}
              onClick={() => void addSuggestion(suggestion.label, suggestion.origin)}
            >
              Add {suggestion.label}
            </Button>
          ))}
        </div>
      )}
      <Button

        type="button"
        data-testid="local-endpoint-discover"
        disabled={busy}
        onClick={() => void discover()}
      >
        <RefreshCw size={14} /> Find local servers
      </Button>
      {!adding ? (
        <Button type="button" data-testid="local-endpoint-add" disabled={busy} onClick={() => setAdding(true)}>
          Add local server
        </Button>
      ) : (
        <form className="form-inline" data-testid="local-endpoint-add-form" onSubmit={add}>
          <FormField label="Name" >

            <TextInput
              data-testid="local-endpoint-add-name"
              aria-label="Local server name"
              value={name}
              disabled={busy}
              placeholder="LM Studio — gaming PC"
              onChange={(event) => setName(event.target.value)}
            />
          </FormField>
          <FormField label="URL" >

            <TextInput
              data-testid="local-endpoint-add-origin"
              type="url"
              aria-label="Local server URL"
              value={url}
              disabled={busy}
              placeholder="http://localhost:1234"
              onChange={(event) => setUrl(event.target.value)}
            />
          </FormField>
          <Button
            className="btn-primary"
            type="submit"
            data-testid="local-endpoint-add-submit"
            disabled={busy || !name.trim() || !url.trim()}
          >
            Add server
          </Button>
          <Button type="button" disabled={busy} onClick={() => setAdding(false)}>
            Cancel
          </Button>
        </form>
      )}
      {notice && (
        <p className="panel-notice" data-tone="success" data-testid="local-endpoint-discovery-summary" role="status">
          {notice}
        </p>
      )}
      {error && <p className="panel-notice" data-tone="error">{error}</p>}
      <ModelPullHistory loadPulls={listModelPulls} />
    </div>
  );
}

function LocalEndpointRow({
  endpoint,
  onReload,
  onRecheck,
}: {
  endpoint: LocalHttpEndpointEntry;
  onReload: () => Promise<void>;
  onRecheck: () => Promise<void>;
}) {
  const [name, setName] = useState(endpoint.label);
  const url = endpoint.origin;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [inferenceToken, setInferenceToken] = useState('');
  const [provisioningToken, setProvisioningToken] = useState('');
  const [lastIdentity, setLastIdentity] = useState(endpoint.label);
  const identity = endpoint.label;
  if (identity !== lastIdentity) {
    setLastIdentity(identity);
    setName(endpoint.label);
  }
  const dirty = name.trim() !== endpoint.label;
  const endpointId = endpoint.endpoint_id;

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || !dirty || !name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await updateLocalEndpoint(endpointId, { display_name: name.trim() });
      await onReload();
    } catch (caught) {
      setError(errMsg(caught));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (busy || endpoint.read_only) return;
    if (!window.confirm(
      `Delete ${endpoint.label}? Existing sheets keep their model IDs, but future runs using this endpoint will fail until you choose another endpoint.`,
    )) return;
    setBusy(true);
    setError(null);
    try {
      await deleteLocalEndpoint(endpointId);
      await onReload();
    } catch (caught) {
      setError(errMsg(caught));
      setBusy(false);
    }
  };

  const saveCredentials = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || (!inferenceToken && !provisioningToken)) return;
    setBusy(true);
    setError(null);
    try {
      const credentialPatch: LocalEndpointPatch = inferenceToken
        ? {
            inference_token: inferenceToken,
            ...(provisioningToken ? { provisioning_token: provisioningToken } : {}),
          }
        : { provisioning_token: provisioningToken };
      await updateLocalEndpoint(endpointId, credentialPatch);
      setInferenceToken('');
      setProvisioningToken('');
      await onReload();
    } catch (caught) {
      setError(errMsg(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="content-stack"
      data-testid={`local-endpoint-${endpointId}`}
      data-tour="local-endpoint-row"
      data-endpoint-id={endpointId}
      data-endpoint-label={endpoint.label}
    >
      <p className="settings-meta-label">
        {endpoint.label}{' '}
        <span data-testid={`local-endpoint-status-${endpointId}`}>
          <StatusChip tone={endpoint.reachable ? 'info' : 'neutral'}>
            {endpoint.reachable ? 'reachable' : 'not running'}
          </StatusChip>
        </span>
      </p>
      <p className="form-hint">
        This URL is the endpoint's immutable identity. To move it to another
        URL, add a new server and then delete this one.
        {endpoint.read_only && ' This endpoint is managed by the server environment and cannot be edited here.'}
      </p>
      <LocalEndpointProbeDetail endpoint={endpoint} />
      <form className="form-inline" onSubmit={save}>
        <FormField label="Name" >

          <TextInput data-testid={`local-endpoint-name-${endpointId}`} value={name} disabled={busy || endpoint.read_only} onChange={(event) => setName(event.target.value)} />
        </FormField>
        <FormField label="URL" >

          <TextInput data-testid={`local-endpoint-origin-${endpointId}`} type="url" value={url} disabled />
        </FormField>
        {!endpoint.read_only && (
          <Button type="submit" disabled={busy || !dirty || !name.trim()}>
            <Save size={14} /> Save
          </Button>
        )}
        <Button type="button" disabled={busy} onClick={() => void onRecheck()}>
          <RefreshCw size={14} /> Recheck
        </Button>
        {!endpoint.read_only && (
          <button className="icon-btn" type="button" disabled={busy} aria-label={`Delete ${endpoint.label}`} onClick={() => void remove()}>
            <Trash2 size={14} />
          </button>
        )}
      </form>
      {!endpoint.read_only && (
        <>
          <form className="form-inline" data-testid={`local-endpoint-auth-${endpointId}`} onSubmit={saveCredentials}>
            <FormField label={<span>Inference token {endpoint.token_configured ? '(configured)' : ''}</span>} >

              <TextInput
                type="password"
                value={inferenceToken}
                autoComplete="new-password"
                placeholder="Leave blank to keep current token"
                disabled={busy}
                onChange={(event) => setInferenceToken(event.target.value)}
              />
            </FormField>
            <FormField label={<span>Download token {endpoint.provisioning_token_configured ? '(configured)' : ''}</span>} >

              <TextInput
                type="password"
                value={provisioningToken}
                autoComplete="new-password"
                placeholder="Leave blank to keep current token"
                disabled={busy}
                onChange={(event) => setProvisioningToken(event.target.value)}
              />
            </FormField>
            <Button type="submit" disabled={busy || (!inferenceToken && !provisioningToken)}>
              Save credentials
            </Button>
            {endpoint.token_configured && (
              <Button type="button" disabled={busy} onClick={() => void updateLocalEndpoint(endpointId, { inference_token: null }).then(onReload).catch((caught) => setError(errMsg(caught)))}>
                Clear inference token
              </Button>
            )}
            {endpoint.provisioning_token_configured && (
              <Button type="button" disabled={busy} onClick={() => void updateLocalEndpoint(endpointId, { provisioning_token: null }).then(onReload).catch((caught) => setError(errMsg(caught)))}>
                Clear download token
              </Button>
            )}
          </form>
          <label className="settings-check">
            <input
              type="checkbox"
              checked={endpoint.edge_auth}
              disabled={busy}
              onChange={() => void updateLocalEndpoint(endpointId, { edge_auth: !endpoint.edge_auth }).then(onReload).catch((caught) => setError(errMsg(caught)))}
            />
            <span>Require the endpoint front door to enforce bearer authentication</span>
          </label>
          <label className="settings-check">
            <input
              type="checkbox"
              checked={endpoint.pull_enabled}
              disabled={busy}
              onChange={() => void updateLocalEndpoint(endpointId, { pull_enabled: !endpoint.pull_enabled }).then(onReload).catch((caught) => setError(errMsg(caught)))}
            />
            <span>Allow model downloads from this server</span>
          </label>
        </>
      )}
      <div
        data-testid={`local-endpoint-guidance-${endpointId}`}
        data-tour="local-endpoint-guidance"
        data-endpoint-label={endpoint.label}
      >
        <LocalServerGuidance entry={endpoint} variant="full" onRecheck={onRecheck} />
      </div>
      {error && <p className="panel-notice" data-tone="error">{error}</p>}
    </div>
  );
}

function LocalEndpointProbeDetail({ endpoint }: { endpoint: LocalHttpEndpointEntry }) {
  const unhealthy = endpoint.reachable === false || endpoint.auth_status === 'unauthorized';
  if (!unhealthy) return null;
  const summary =
    endpoint.auth_status === 'unauthorized'
      ? `${endpoint.label} rejected authentication at ${endpoint.origin}`
      : `${endpoint.label} is not running at ${endpoint.origin}`;
  if (!endpoint.detail) {
    return (
      <p className="form-hint" data-testid={`local-endpoint-probe-summary-${endpoint.endpoint_id}`}>
        {summary}.
      </p>
    );
  }
  return (
    <details className="settings-probe-detail" data-testid={`local-endpoint-probe-detail-${endpoint.endpoint_id}`}>
      <summary data-testid={`local-endpoint-probe-summary-${endpoint.endpoint_id}`}>{summary}</summary>
      <code className="mono" data-testid={`local-endpoint-probe-detail-text-${endpoint.endpoint_id}`}>
        {endpoint.detail}
      </code>
    </details>
  );
}
