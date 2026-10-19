import '../components/ApplicationGuidance.css';
import { Button } from '../components/Button';
import { FormField, TextInput } from '../components/FormControls';
import { type FormEvent, useState } from 'react';
import { Check, Copy, Download, KeyRound, Save, Trash2 } from 'lucide-react';
import { ProviderKeyForm } from '../engine-selector/ProviderKeyForm';
import {
  ApiError,
  createApiKey,
  deleteOrgEnvVar,
  deleteOrgKey,
  listOrgLocalEndpoints,
  getSpend,
  listApiKeys,
  listOrgEnvVars,
  listOrgKeys,
  orgCancelModelPull,
  orgGetModelPull,
  orgListModelPulls,
  orgStartArtifactPull,
  providerCatalog,
  revokeApiKey,
  setOrgEnvVar,
  setOrgKey,
  validateOrgKey,
  type ApiTokenInfo,
  type LocalHttpEndpointEntry,
  type ModelPullDto,
  type LocalEndpointCatalog,
  type OrgEnvInfo,
  type OrgKeyInfo,
  type ProviderCatalog,
  type SpendReport,
} from '../api/open';
import { ModelPullProgress } from '../components/ModelPullProgress';
import { PanelError, StatusChip, type StatusTone } from '../components/PanelPrimitives';
import { formatUsdOrNone } from '../format';
import { ModelPullHistory } from './ModelPullHistory';
import {
  useLoader,
  type ProviderValidationTone,
  providerValidationMessage,
  providerValidationTone,
  errMsg,
} from './settingsSupport';
import { InlineStatus, SettingsTable } from './SettingsPrimitives';

export function OrganizationAiProvidersSettings() {
  const catalog = useLoader<ProviderCatalog>(() => providerCatalog());
  const keys = useLoader<OrgKeyInfo[]>(() => listOrgKeys());
  const [adding, setAdding] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [existingTests, setExistingTests] = useState<
    Record<string, { text: string; tone: ProviderValidationTone | 'pending' }>
  >({});

  const rows = keys.data ?? [];
  const testExisting = async (provider: string) => {
    setExistingTests((current) => ({ ...current, [provider]: { text: 'Testing...', tone: 'pending' } }));
    try {
      const result = await validateOrgKey(provider);
      setExistingTests((current) => ({
        ...current,
        [provider]: { text: providerValidationMessage(result), tone: providerValidationTone(result) },
      }));
    } catch (caught) {
      setExistingTests((current) => ({ ...current, [provider]: { text: errMsg(caught), tone: 'error' } }));
    }
  };
  return (
    <div className="content-stack" data-testid="organization-ai-providers-settings">
      <InlineStatus error={catalog.error ?? keys.error ?? mutationError} loading={catalog.loading || keys.loading} notice={notice} />
      <Button type="button" onClick={() => { setNotice(null); setMutationError(null); setAdding((current) => !current); }}>
        <KeyRound size={14} /> Add new key
      </Button>
      {adding && <ProviderKeyForm canMutate keyLabel="Provider key" providers={catalog.data?.providers ?? []}
        validate={(provider, key, signal) => validateOrgKey(provider, key, { signal })}
        save={(provider, key, receipt, _cap, signal) => setOrgKey(provider, key, receipt, { signal })}
        onSaved={() => { setAdding(false); setNotice('Provider key saved'); void keys.reload(); }} />}
      <SettingsTable
        headers={['Provider', 'Key', '']}
        rows={rows.map((row) => [
          row.provider,
          <>
            <span>{row.hint}</span>
            {existingTests[row.provider] && (
              <div
                className="panel-notice" data-tone={existingTests[row.provider].tone === 'pending' ? 'neutral' : existingTests[row.provider].tone}
                data-testid={`provider-existing-validation-${row.provider}`}
              >
                {existingTests[row.provider].text}
              </div>
            )}
          </>,
          <span key={row.provider} className="control-row">
            <Button
              className="btn-compact"
              type="button"
              aria-label={`Test ${row.provider} provider key`}
              onClick={() => void testExisting(row.provider)}
            >
              Test
            </Button>
            <button
              className="icon-btn"
              type="button"
              aria-label={`Delete ${row.provider} provider key`}
              title={`Delete ${row.provider} provider key`}
              onClick={() => void deleteOrgKey(row.provider)
                .then(keys.reload)
                .catch((caught) => setMutationError(errMsg(caught)))}
            >
              <Trash2 size={14} />
            </button>
          </span>,
        ])}
        empty="No provider keys"
      />
      <OrgLocalModelsSettings />
    </div>
  );
}

// Org (team-tier) local endpoint cards come from the same plural catalog DTO
// as workspace endpoints, reported under the hosted org authority by
// GET /api/org/local-endpoints. An empty collection means the operator has
// not wired up an endpoint, so nothing renders (unlike workspace guidance for
// a machine the user controls). The UI has no way to know owner-vs-member, so
// a member's forbidden download surfaces through the flow's error message.
function isModelPullDto(value: unknown): value is ModelPullDto {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { id?: unknown }).id === 'number' &&
    typeof (value as { status?: unknown }).status === 'string'
  );
}

function orgLocalModelsStatusChip(
  info: LocalHttpEndpointEntry,
): { tone: StatusTone; text: string } {
  if (info.auth_status === 'unenforced') {
    return {
      tone: 'warning',
      text: 'token configured but not enforced — check your front door',
    };
  }
  if (info.auth_status === 'unauthorized') {
    return { tone: 'neutral', text: 'authentication required' };
  }
  if (info.reachable === false) {
    return { tone: 'neutral', text: 'not reachable' };
  }
  if (info.reachable === true) {
    return { tone: 'info', text: 'reachable' };
  }
  return { tone: 'neutral', text: 'unknown' };
}

function OrgLocalModelsSettings() {
  const info = useLoader<LocalEndpointCatalog>(() => listOrgLocalEndpoints());
  const data = info.data;

  if (info.loading) return null;
  if (!data) {
    if (!info.error) return null;
    return (
      <PanelError testId="org-local-models-fetch-error">
        {info.error}
      </PanelError>
    );
  }
  const endpoint = data.endpoints[0];
  if (!endpoint) return null;

  const status = orgLocalModelsStatusChip(endpoint);
  const models = endpoint.models;

  return (
    <div className="content-stack org-local-models" data-testid="org-local-models-settings">
      <p className="settings-meta-label">
        {endpoint.label}{endpoint.origin ? ` — ${endpoint.origin}` : ''}{' '}
        <StatusChip tone={status.tone} testId="org-local-models-status">
          {status.text}
        </StatusChip>
      </p>
      {endpoint.protocol && (
        <p className="form-hint" data-testid="org-local-models-protocol">
          Protocol: {endpoint.protocol}
        </p>
      )}
      {models.length > 0 ? (
        <ul className="org-local-models-list" data-testid="org-local-models-list">
          {models.map((model) => <li key={model.id}>{model.label}</li>)}
        </ul>
      ) : (
        <p className="form-hint" data-testid="org-local-models-empty">No models installed</p>
      )}
      {endpoint.pull_enabled ? (
        <OrgModelPullDownload endpointId={endpoint.endpoint_id} onInstalled={info.reload} />
      ) : (
        <p className="form-hint" data-testid="org-local-models-pull-disabled">
          Model downloads are disabled; set FRISKET_ENABLE_MODEL_PULL on the server.
        </p>
      )}
      <ModelPullHistory loadPulls={orgListModelPulls} idPrefix="org-"
        fetchPull={orgGetModelPull} cancelPull={orgCancelModelPull} />
    </div>
  );
}

type OrgDownloadPhase = 'idle' | 'confirm' | 'active' | 'error';

function OrgModelPullDownload({
  endpointId,
  onInstalled,
}: {
  endpointId: string;
  onInstalled: () => Promise<void>;
}) {
  const [modelInput, setModelInput] = useState('');
  const [phase, setPhase] = useState<OrgDownloadPhase>('idle');
  const [busy, setBusy] = useState(false);
  const [pull, setPull] = useState<ModelPullDto | null>(null);
  const [error, setError] = useState<string | null>(null);

  const startDownload = async () => {
    const model = modelInput.trim();
    if (!model) return;
    setBusy(true);
    setError(null);
    try {
      const result = await orgStartArtifactPull(`ollama/@${endpointId}/${model}`);
      setPull(result.pull);
      setPhase('active');
    } catch (caught) {
      // pull_busy (409): another owner's pull for this org is already running
      // -- show ITS progress instead of a bare error, same disposition as
      // LocalServerGuidance's startDownload.
      if (caught instanceof ApiError && caught.code === 'pull_busy' && isModelPullDto(caught.details?.active)) {
        setPull(caught.details.active);
        setPhase('active');
      } else {
        // Includes a member's 403 model_pull_disabled/forbidden attempt: the
        // UI cannot know owner-vs-member ahead of time, so that error simply
        // surfaces here as the failure message rather than a pre-hidden button.
        setError(errMsg(caught));
        setPhase('error');
      }
    } finally {
      setBusy(false);
    }
  };

  if (phase === 'active' && pull) {
    return (
      <div className="content-stack org-model-pull-download" data-testid="org-model-pull-download">
        <ModelPullProgress
          pull={pull}
          fetchPull={orgGetModelPull}
          cancelPull={orgCancelModelPull}
          onDone={() => {
            setPhase('idle');
            setPull(null);
            setModelInput('');
            void onInstalled();
          }}
        />
      </div>
    );
  }

  return (
    <div className="content-stack org-model-pull-download" data-testid="org-model-pull-download">
      <FormField label="Download a model" >

        <TextInput
          type="text"
          aria-label="Model name"
          data-testid="org-model-pull-input"
          placeholder="e.g. qwen3:8b"
          value={modelInput}
          disabled={busy || phase === 'confirm'}
          onChange={(event) => setModelInput(event.target.value)}
        />
      </FormField>
      {phase === 'confirm' ? (
        <div className="control-row">
          <p className="local-server-guidance-copy">
            This may download several GB to the operator&rsquo;s local server.
          </p>
          <Button
            type="button"
            className="btn-primary"
            data-testid="org-model-pull-confirm"
            disabled={busy}
            onClick={() => void startDownload()}
          >
            Confirm
          </Button>
          <Button
            type="button"

            data-testid="org-model-pull-cancel-confirm"
            disabled={busy}
            onClick={() => setPhase('idle')}
          >
            Cancel
          </Button>
        </div>
      ) : (
        <Button
          type="button"
          className="btn-primary"
          data-testid="org-model-pull-start"
          disabled={!modelInput.trim()}
          onClick={() => setPhase('confirm')}
        >
          <Download size={14} /> Download
        </Button>
      )}
      {phase === 'error' && error && (
        <p
          className="panel-notice" data-tone="error"
          data-testid="org-model-pull-error"
        >
          {error}
        </p>
      )}
    </div>
  );
}


export function OrganizationSecretsSettings() {
  const secrets = useLoader<OrgEnvInfo[]>(() => listOrgEnvVars());
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const save = async (event: FormEvent) => {
    event.preventDefault();
    setMutationError(null);
    try {
      await setOrgEnvVar(name, value);
      setName('');
      setValue('');
      setNotice('Secret saved');
      await secrets.reload();
    } catch (caught) {
      setNotice(null);
      setMutationError(errMsg(caught));
    }
  };
  return (
    <div className="content-stack" data-testid="organization-secrets-settings">
      <InlineStatus error={secrets.error ?? mutationError} loading={secrets.loading} notice={notice} />
      <form className="form-inline" autoComplete="off" onSubmit={save}>
        <TextInput
          aria-label="Secret name"
          autoComplete="off"
          placeholder="Name"
          value={name}
          onChange={(event) => setName(event.currentTarget.value)}
        />
        <TextInput
          aria-label="Secret value"
          type="password"
          autoComplete="new-password"
          placeholder="Value"
          value={value}
          onChange={(event) => setValue(event.currentTarget.value)}
        />
        <Button className="btn-primary" type="submit" disabled={!name.trim() || !value}>
          <Save size={14} /> Save
        </Button>
      </form>
      <SettingsTable
        headers={['Name', 'Value', 'Updated', '']}
        rows={(secrets.data ?? []).map((row) => [
          row.name,
          row.hint,
          row.created_at ?? '-',
          <button
            key={row.name}
            className="icon-btn"
            type="button"
            aria-label={`Delete ${row.name} secret`}
            title={`Delete ${row.name} secret`}
            onClick={() => void deleteOrgEnvVar(row.name)
              .then(secrets.reload)
              .catch((caught) => setMutationError(errMsg(caught)))}
          >
            <Trash2 size={14} />
          </button>,
        ])}
        empty="No organization secrets"
      />
    </div>
  );
}

export function OrganizationApiKeysSettings() {
  const tokens = useLoader<ApiTokenInfo[]>(() => listApiKeys());
  const [name, setName] = useState('');
  const [created, setCreated] = useState<{ token: string } | null>(null);
  const [copyStatus, setCopyStatus] = useState<'idle' | 'copied' | 'failed'>('idle');
  const [mutationError, setMutationError] = useState<string | null>(null);
  const save = async (event: FormEvent) => {
    event.preventDefault();
    setMutationError(null);
    setCopyStatus('idle');
    try {
      const response = await createApiKey(name);
      setCreated({ token: response.token });
      setName('');
      await tokens.reload();
    } catch (caught) {
      setCreated(null);
      setMutationError(errMsg(caught));
    }
  };
  const copyCreatedToken = async () => {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created.token);
      setCopyStatus('copied');
    } catch {
      setCopyStatus('failed');
    }
  };
  return (
    <div className="content-stack" data-testid="organization-api-keys-settings">
      <InlineStatus error={tokens.error ?? mutationError} loading={tokens.loading} />
      {created && (
        <output className="settings-token-reveal" data-testid="api-key-created" aria-live="polite">
          <div className="settings-token-reveal-head">
            <div>
              <strong>Copy this API token now</strong>
              <span>It is shown once. The token list keeps only a redacted prefix.</span>
            </div>
          </div>
          <div className="settings-token-copy-row">
            <code className="settings-token-secret" data-testid="api-key-created-token">{created.token}</code>
            <Button type="button" onClick={() => void copyCreatedToken()}>
              {copyStatus === 'copied' ? <Check size={14} /> : <Copy size={14} />}
              {copyStatus === 'copied' ? 'Copied' : 'Copy'}
            </Button>
          </div>
          {copyStatus === 'failed' && (
            <div className="settings-copy-status">Copy unavailable</div>
          )}
        </output>
      )}
      <form className="form-inline" onSubmit={save}>
        <TextInput aria-label="Token name" placeholder="Token name" value={name} onChange={(event) => setName(event.currentTarget.value)} />
        <Button className="btn-primary" type="submit" disabled={!name.trim()}>
          <KeyRound size={14} /> Create
        </Button>
      </form>
      <SettingsTable
        headers={['Name', 'Prefix', 'Created by', 'Last used', '']}
        rows={(tokens.data ?? []).map((row) => [
          row.name,
          <code key={`${row.id}:prefix`} className="settings-token-prefix" data-testid={`api-token-prefix-${row.id}`}>
            {row.prefix}...redacted
          </code>,
          row.created_by,
          row.last_used_at ?? '-',
          row.revoked ? 'Revoked' : (
            <Button
              key={row.id}

              type="button"
              onClick={() => void revokeApiKey(row.id)
                .then(tokens.reload)
                .catch((caught) => setMutationError(errMsg(caught)))}
            >
              Revoke
            </Button>
          ),
        ])}
        empty="No API keys"
      />
    </div>
  );
}

export function OrganizationSpendSettings() {
  const spend = useLoader<SpendReport>(() => getSpend());
  return (
    <div className="content-stack" data-testid="organization-spend-settings">
      <InlineStatus error={spend.error} loading={spend.loading} />
      <SettingsTable
        headers={['Project', 'Model', 'Month', 'Runs', 'Rows', 'Cost']}
        rows={(spend.data?.rows ?? []).map((row) => [
          row.project,
          row.model ?? '-',
          row.month ?? '-',
          row.runs,
          row.rows,
          formatUsdOrNone(row.cost),
        ])}
        empty="No usage"
      />
      <div className="settings-total">Total {formatUsdOrNone(spend.data?.total_cost ?? 0)}</div>
    </div>
  );
}
