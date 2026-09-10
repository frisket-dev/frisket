import {
  type FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from 'react';
import {
  AlertTriangle,
  Archive,
  Check,
  CheckCircle2,
  Copy,
  Database,
  Download,
  Eye,
  EyeOff,
  Info,
  KeyRound,
  RefreshCw,
  Save,
  Trash2,
  XCircle,
} from 'lucide-react';
import { LocalServerGuidance } from '../components/LocalServerGuidance';
export { McpServersSection } from './McpServersSection';
import { McpServersSection } from './McpServersSection';
import type { LocalEndpointPatch } from '../api/localProviders';
import {
  ApiError,
  createLocalEndpoint,
  createApiKey,
  discoverLocalEndpoints,
  deleteLocalEndpoint,
  deleteOrgEnvVar,
  deleteOrgKey,
  deleteProviderKey,
  getMe,
  listOrgLocalEndpoints,
  getSpend,
  listApiKeys,
  listModelPulls,
  listOrgEnvVars,
  listOrgKeys,
  listProviders,
  providerStatus,
  orgCancelModelPull,
  orgGetModelPull,
  orgListModelPulls,
  orgStartArtifactPull,
  setProviderKey,
  validateProviderKey,
  listProjectInvites,
  listProjectMembers,
  providerCatalog,
  revokeApiKey,
  revokeProjectInvite,
  setOrgEnvVar,
  setOrgKey,
  setProjectMember,
  createProjectInvite,
  getRuntimeConfig,
  updateRuntimeConfig,
  updateLocalCostPreapproval,
  removeProjectMember,
  signOut,
  updateProfile,
  updateLocalEndpoint,
  validateOrgKey,
  type ApiTokenInfo,
  type LocalProviderCatalog,
  type LocalProviderEntry,
  type LocalEndpointDiscoveryCandidate,
  type LocalHttpEndpointEntry,
  type ModelPullDto,
  type LocalEndpointCatalog,
  type RuntimeConfig,
  type MeInfo,
  type OrgEnvInfo,
  type OrgKeyInfo,
  type ProjectInfo,
  type ProjectInvite,
  type ProjectInviteRole,
  type ProjectMember,
  type ProjectNetworkPolicy,
  type ProjectProviderKeyInfo,
  type ProjectProviderKeys,
  type ProjectRetentionPolicy,
  type ProjectSettings,
  type ProjectRole,
  type ProjectSecrets,
  type ProviderCatalog,
  type ProviderValidateResult,
  type SpendReport,
  type WorkbenchPluginRuntimeIndex,
  type WorkbenchPluginSettings,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import {
  canEditProject,
  canOwnProject,
  canReviewProject,
  projectRole,
} from '../api/projectRole';
import { ModelPullProgress } from '../components/ModelPullProgress';
import { NotificationSettingsPanel } from '../components/NotificationSettingsPanel';
import { PanelSelect, type PanelSelectOption } from '../components/PanelSelect';
import { ConfirmTypeInput, StatusChip, type StatusTone } from '../components/PanelPrimitives';
import { formatNumberDisplay, formatUsdOrNone } from '../format';
import { navigate, type SettingsRoute } from '../routes';
import { PluginManager } from '../workbench/PluginManager';
import type {
  OpenSettingsSectionDefinition,
  SettingsSectionDefinition,
} from './settingsRegistry';
import {
  applyPersonalPreferences,
  PREFERENCES_KEY,
  readPersonalPreferences,
  type Preferences,
} from './preferences';
import { readSettingsProjectContext } from './settingsProjectContext';
import { fetchDiagnostics, type DiagnosticsReport } from '../api/diagnostics';
import {
  sendProductTelemetry,
  setTelemetryPreference,
  telemetryPreference,
} from '../telemetry/productTelemetry';

const MEMBER_ROLES: ProjectRole[] = ['viewer', 'reviewer', 'editor', 'owner'];
const INVITE_ROLES: ProjectInviteRole[] = ['viewer', 'editor'];
const FALLBACK_EVIDENCE_RETENTION_VALUES = ['compactable', 'pinned', 'materialized'] as const;
const EVIDENCE_RETENTION_LABELS: Record<string, string> = {
  compactable: 'Compactable',
  pinned: 'Pinned',
  materialized: 'Materialized',
};
const EVIDENCE_RETENTION_DESCRIPTIONS: Record<string, string> = {
  compactable: 'Save space. Manual compaction may remove supporting run evidence; sheet results stay.',
  pinned: 'Keep supporting run evidence when this project is manually compacted.',
  materialized: 'Keep output-linked evidence as part of the stored result.',
};

function errMsg(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function accessErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError && (error.status === 403 || error.status === 404)) {
    return fallback;
  }
  return errMsg(error);
}

function isFreshAuthError(error: unknown): boolean {
  return error instanceof ApiError &&
    error.status === 403 &&
    /fresh|reauth/i.test(error.message);
}

function formatDate(value: string | null | undefined): string {
  return value || '-';
}

function safeTestKey(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'unknown';
}

function memberKey(member: ProjectMember): string {
  return Number.isFinite(member.user_id) ? String(member.user_id) : safeTestKey(member.email);
}

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
 * keys, and LLM calls outside a run (copilot, preview translation, sandbox
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

function formatBytes(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${value} bytes` : '0 bytes';
}

function plural(value: unknown, singular: string): string {
  return value === 1 ? singular : `${singular}s`;
}

function supportedEvidenceOptions(policy: ProjectRetentionPolicy | null): string[] {
  const raw = Array.isArray(policy?.supported_default_evidence)
    ? policy.supported_default_evidence
    : [...FALLBACK_EVIDENCE_RETENTION_VALUES];
  const supported = raw.filter((value): value is string => (
    typeof value === 'string' && value in EVIDENCE_RETENTION_LABELS
  ));
  return supported.length ? supported : [...FALLBACK_EVIDENCE_RETENTION_VALUES];
}

function useLoader<T>(
  load: () => Promise<T>,
  reloadKey: string | number | boolean = '',
): {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => Promise<void>;
  setData: (value: T) => void;
} {
  type LS = { data: T | null; error: string | null; loading: boolean };
  type LA = { type: 'start' } | { type: 'ok'; value: T } | { type: 'fail'; error: string } | { type: 'put'; value: T };
  const loadRef = useRef(load);
  const [ls, dispatch] = useReducer(
    (s: LS, a: LA): LS => {
      if (a.type === 'start') return { ...s, loading: true };
      if (a.type === 'ok') return { data: a.value, error: null, loading: false };
      if (a.type === 'fail') return { ...s, error: a.error, loading: false };
      return { ...s, data: a.value };
    },
    { data: null, error: null, loading: true },
  );
  useEffect(() => {
    loadRef.current = load;
  });
  const reload = useCallback((): Promise<void> => {
    dispatch({ type: 'start' });
    return loadRef.current().then(
      (value) => { dispatch({ type: 'ok', value }); },
      (caught: unknown) => { dispatch({ type: 'fail', error: errMsg(caught) }); },
    );
  }, []);
  useEffect(() => {
    let alive = true;
    // Microtask deferral + alive check preserve HEAD semantics (StrictMode
    // double-invoke starts at most one live fetch), and reloadKey changes
    // show the loading state again.
    void Promise.resolve().then(() => {
      if (!alive) return;
      dispatch({ type: 'start' });
      return loadRef.current().then(
        (value) => { if (alive) dispatch({ type: 'ok', value }); },
        (caught: unknown) => { if (alive) dispatch({ type: 'fail', error: errMsg(caught) }); },
      );
    });
    return () => { alive = false; };
  }, [reloadKey]);
  return { data: ls.data, error: ls.error, loading: ls.loading, reload, setData: (value: T) => dispatch({ type: 'put', value }) };
}

/** The ONE settings section frame: kicker + h1 + summary inside
 *  .settings-section-frame. The disabled / invalid / route-error sections in
 *  SettingsWorkspace.tsx render through it too, so the frame's
 *  padding/typography can never fork per state. Pass `definition` for a real
 *  registry section (testid/kicker/title/summary derive from it, each
 *  overridable), or the explicit props for the stateful variants. */
export function SectionFrame({
  definition,
  kicker,
  title,
  summary,
  testId,
  className,
  children,
}: {
  definition?: SettingsSectionDefinition;
  kicker?: string;
  title?: string;
  summary?: ReactNode;
  testId?: string;
  className?: string;
  children?: ReactNode;
}) {
  const classes = ['settings-section-frame', className].filter(Boolean).join(' ');
  const resolvedTestId =
    testId ??
    (definition ? `settings-section-${definition.scope}-${definition.section}` : undefined);
  return (
    <section
      className={classes}
      data-testid={resolvedTestId}
      data-settings-section-id={definition?.id}
    >
      <div className="settings-section-kicker">{kicker ?? definition?.navGroup}</div>
      <h1>{title ?? definition?.title}</h1>
      <p className="settings-section-summary">{summary ?? definition?.summary}</p>
      {children}
    </section>
  );
}

function InlineStatus({
  error,
  notice,
  loading,
}: {
  error?: string | null;
  notice?: string | null;
  loading?: boolean;
}) {
  if (loading) return <div className="settings-inline-status">Loading...</div>;
  if (error) return <div className="settings-inline-error" role="alert">{error}</div>;
  if (notice) return <div className="settings-inline-status">{notice}</div>;
  return null;
}

function ReadOnlyNotice({ children }: { children: ReactNode }) {
  return <div className="settings-readonly-note">{children}</div>;
}

export function PersonalProfileSettings({ identityMode }: { identityMode: boolean }) {
  const { data: me, error, loading, setData } = useLoader<MeInfo>(
    async () => {
      try {
        return await getMe();
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 404) {
          return {
            email: 'Local workspace',
            display_name: localStorage.getItem('frisket.personal_display_name') ?? '',
          };
        }
        throw caught;
      }
    },
  );
  const [displayName, setDisplayName] = useState<string | null>(null);
  const [costPreapproval, setCostPreapproval] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const runtime = useLoader<RuntimeConfig>(() => getRuntimeConfig());
  const displayNameValue = displayName ?? me?.display_name ?? '';
  const costPreapprovalValue = costPreapproval
    ?? (identityMode
      ? me?.cost_preapproval_usd ?? '2'
      : runtime.data?.cost_preapproval_usd ?? '2');
  const displayNameChanged = displayName !== null && displayName !== (me?.display_name ?? '');
  const persistedCostPreapproval = identityMode
    ? me?.cost_preapproval_usd
    : runtime.data?.cost_preapproval_usd;
  const costPreapprovalChanged = costPreapproval !== null
    && costPreapproval !== (persistedCostPreapproval ?? '');
  const localRuntimeLoading = !identityMode && runtime.loading;

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (localRuntimeLoading) return;
    setBusy(true);
    try {
      let updated: MeInfo = me ?? {
        email: 'Local workspace',
        display_name: '',
      };
      if (identityMode) {
        const patch = {
          ...(displayNameChanged ? { display_name: displayNameValue } : {}),
          ...(costPreapprovalChanged ? { cost_preapproval_usd: costPreapprovalValue } : {}),
        };
        if (Object.keys(patch).length) updated = await updateProfile(patch);
      } else {
        if (costPreapprovalChanged) {
          runtime.setData(await updateLocalCostPreapproval(costPreapprovalValue));
        }
        updated = {
          email: me?.email ?? 'Local workspace',
          display_name: displayNameValue,
        };
      }
      setData(updated);
      if (displayNameChanged) {
        localStorage.setItem('frisket.personal_display_name', updated.display_name ?? '');
      }
      setDisplayName(null);
      setCostPreapproval(null);
      setNotice('Profile saved');
    } catch (caught) {
      setNotice(errMsg(caught));
    } finally {
      setBusy(false);
    }
  };

  const initials = (me?.display_name || me?.email || '?').slice(0, 2).toUpperCase();

  return (
    <div className="settings-stack" data-testid="personal-profile-settings">
      <InlineStatus error={error ?? runtime.error} loading={loading || localRuntimeLoading} notice={notice} />
      <div className="settings-profile-row">
        <div className="settings-avatar" data-testid="settings-avatar">{initials}</div>
        <div>
          <div className="settings-meta-label">Email</div>
          <strong>{me?.email ?? 'Local workspace'}</strong>
        </div>
      </div>
      <form className="settings-form" onSubmit={save}>
        <label>
          <span>Display name</span>
          <input
            data-testid="personal-display-name"
            value={displayNameValue}
            maxLength={200}
            onChange={(event) => setDisplayName(event.currentTarget.value)}
          />
        </label>
        <label>
          <span>Cost pre-approval amount (USD)</span>
          <input
            data-testid="personal-cost-preapproval"
            type="number"
            min="0"
            step="0.000001"
            inputMode="decimal"
            value={costPreapprovalValue}
            onChange={(event) => setCostPreapproval(event.currentTarget.value)}
          />
          <small className="settings-help">
            Frisket always shows an estimate. Runs at or below this amount can use
            their selected provider without another prompt; higher or unknown costs
            require one click.
          </small>
        </label>
        <div className="settings-actions">
          <button className="btn btn-primary" type="submit" disabled={busy || localRuntimeLoading}>
            <Save size={14} /> Save
          </button>
          <button
            className="btn"
            type="button"
            data-testid="personal-sign-out"
            onClick={() => {
              void signOut().then(() => window.location.reload());
            }}
          >
            Sign out
          </button>
        </div>
      </form>
    </div>
  );
}

// Order matches the server's cache_mode enum. The local instance persists a
// deliberate choice; team/managed compositions report this setting read-only.
const AI_CALL_MODES: ReadonlyArray<{
  value: RuntimeConfig['cache_mode'];
  label: string;
  help: string;
}> = [
  {
    value: 'replay',
    label: 'Replay',
    help: 'Cached responses are reused; a request with no cached response makes a live AI call and caches it. The default.',
  },
  {
    value: 'replay_strict',
    label: 'Strict replay',
    help: 'No live AI calls: cached responses are reused, and a request with no cached response fails instead of calling a provider.',
  },
  {
    value: 'fresh',
    label: 'Live with cache',
    help: 'Every request makes a live AI call and caches the response, so later identical requests are reused for free.',
  },
  {
    value: 'off',
    label: 'Live without cache',
    help: 'Every request makes a live AI call. Responses are not cached.',
  },
];

export function AiCallModeSettings() {
  const [config, setConfig] = useState<RuntimeConfig | null>(null);
  const [selected, setSelected] = useState<RuntimeConfig['cache_mode'] | null>(null);
  const [pendingLiveMode, setPendingLiveMode] = useState<RuntimeConfig['cache_mode'] | null>(null);
  const [confirmText, setConfirmText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    getRuntimeConfig()
      .then((loaded) => {
        if (alive) {
          setConfig(loaded);
          setSelected(loaded.cache_mode);
        }
      })
      .catch((loadError) => {
        if (alive) setError(errMsg(loadError));
      });
    return () => {
      alive = false;
    };
  }, []);
  const current = config?.cache_mode;
  const currentMode = AI_CALL_MODES.find((mode) => mode.value === current);
  const editable = config ? config.cache_mode_editable : false;

  const save = async (
    mode: RuntimeConfig['cache_mode'],
    confirmed: boolean,
  ) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await updateRuntimeConfig(mode, confirmed);
      setConfig(updated);
      setSelected(updated.cache_mode);
      setPendingLiveMode(null);
      setConfirmText('');
      setNotice(`AI call mode changed to ${AI_CALL_MODES.find((item) => item.value === updated.cache_mode)?.label ?? updated.cache_mode}.`);
    } catch (saveError) {
      setError(errMsg(saveError));
    } finally {
      setBusy(false);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!config || !selected || selected === current || busy) return;
    if (selected !== 'replay_strict') {
      setPendingLiveMode(selected);
      setConfirmText('');
      return;
    }
    void save(selected, false);
  };

  return (
    <section className="settings-subsection ai-call-mode-settings" data-testid="ai-call-mode-settings">
      <div className="settings-section-kicker">AI call mode</div>
      <InlineStatus error={error} loading={config === null && error === null} notice={notice} />
      <p className="settings-help">
        Whether frisket makes live AI calls or reuses cached responses.
        {currentMode ? (
          <>
            {' '}The server is currently in <strong>{currentMode.label}</strong> mode.
          </>
        ) : null}{' '}
        {editable ? (
          <>Changes apply to new runs and persist for this local workspace.</>
        ) : (
          <>
            Set by the <code>FRISKET_CACHE_MODE</code> environment variable
            (default <code>replay</code>). <AiCallModeChangeHint config={config} />
          </>
        )}
      </p>
      {editable ? (
        <form className="settings-form" onSubmit={submit}>
          <fieldset
            className="ai-call-mode-list ai-call-mode-options"
            data-testid="ai-call-mode-list"
            disabled={busy || config === null}
          >
            <legend className="sr-only">Choose AI call mode</legend>
            {AI_CALL_MODES.map((mode) => (
              <label
                key={mode.value}
                className={`ai-call-mode-item ai-call-mode-option${mode.value === current ? ' is-current' : ''}`}
                data-testid={`ai-call-mode-${mode.value}`}
              >
                <input
                  type="radio"
                  name="ai-call-mode"
                  value={mode.value}
                  checked={selected === mode.value}
                  onChange={() => setSelected(mode.value)}
                />
                <span>
                  <span className="ai-call-mode-name">
                    {mode.label}
                    {mode.value === current && <span className="ai-call-mode-badge">Current</span>}
                  </span>
                  <span className="ai-call-mode-help">{mode.help}</span>
                </span>
              </label>
            ))}
          </fieldset>
          <div className="settings-actions">
            <button
              type="submit"
              className="btn btn-primary"
              data-testid="ai-call-mode-save"
              disabled={busy || selected === null || selected === current}
            >
              <Save size={14} /> {busy ? 'Saving…' : 'Save mode'}
            </button>
          </div>
        </form>
      ) : (
        <dl className="ai-call-mode-list" data-testid="ai-call-mode-list">
          {AI_CALL_MODES.map((mode) => (
            <div
              key={mode.value}
              className={`ai-call-mode-item${mode.value === current ? ' is-current' : ''}`}
              data-testid={`ai-call-mode-${mode.value}`}
            >
              <dt className="ai-call-mode-name">
                {mode.label}
                {mode.value === current && <span className="ai-call-mode-badge">Current</span>}
              </dt>
              <dd className="ai-call-mode-help">{mode.help}</dd>
            </div>
          ))}
        </dl>
      )}
      {pendingLiveMode && (
        <div className="modal-backdrop" data-testid="ai-call-mode-confirmation">
          <form
            className="modal-card"
            onSubmit={(event) => {
              event.preventDefault();
              if (confirmText.trim().toLowerCase() === 'confirm') {
                void save(pendingLiveMode, true);
              }
            }}
          >
            <div className="modal-title">
              <AlertTriangle size={15} className="modal-warn-icon" /> Enable live AI calls?
            </div>
            <p className="modal-body-text">
              {AI_CALL_MODES.find((mode) => mode.value === pendingLiveMode)?.label ?? pendingLiveMode}
              {' '}can contact configured AI providers and spend money. Individual paid runs keep their normal cost confirmation.
            </p>
            <label className="form-label" htmlFor="ai-call-mode-confirm-input">
              Type <strong>confirm</strong> to continue.
            </label>
            <ConfirmTypeInput
              id="ai-call-mode-confirm-input"
              className="form-input"
              data-testid="ai-call-mode-confirm-input"
              value={confirmText}
              placeholder="confirm"
              disabled={busy}
              onChange={setConfirmText}
            />
            <div className="form-actions">
              <button
                type="button"
                className="btn"
                data-testid="ai-call-mode-confirm-cancel"
                disabled={busy}
                onClick={() => {
                  setPendingLiveMode(null);
                  setConfirmText('');
                }}
              >
                Cancel
              </button>
              <button
                type="submit"
                className="btn btn-primary"
                data-testid="ai-call-mode-confirm"
                disabled={busy || confirmText.trim().toLowerCase() !== 'confirm'}
              >
                Enable live calls
              </button>
            </div>
          </form>
        </div>
      )}
    </section>
  );
}

/** How to change FRISKET_CACHE_MODE on THIS install.
 *
 *  The old copy hardcoded "edit /srv/frisket/.env and restart the server with
 *  docker compose restart" — the compose path, wrong for anyone who launched
 *  the server directly. `/api/config` reports whether it runs in a container;
 *  a server that does not report it (older, or a hosted deployment) gets the
 *  mechanism without an invented path. */
function AiCallModeChangeHint({ config }: { config: RuntimeConfig | null }) {
  if (config?.in_container === true) {
    return (
      <span data-testid="ai-call-mode-change-hint" data-install-mode="container">
        To change it, set it in your deployment&rsquo;s <code>.env</code> and restart
        the server with <code>docker&nbsp;compose&nbsp;restart</code>.
      </span>
    );
  }
  if (config?.in_container === false) {
    return (
      <span data-testid="ai-call-mode-change-hint" data-install-mode="process">
        To change it, set it in the environment you start frisket from and restart
        the server — e.g. <code>FRISKET_CACHE_MODE=fresh frisket&nbsp;my-workspace</code>.
      </span>
    );
  }
  return (
    <span data-testid="ai-call-mode-change-hint" data-install-mode="unknown">
      To change it, set it in the server&rsquo;s environment and restart the server.
    </span>
  );
}

function PersonalPreferencesSettings() {
  const [prefs, setPrefs] = useState<Preferences>(() => readPersonalPreferences());
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    applyPersonalPreferences(prefs);
  }, [prefs]);

  const update = (patch: Partial<Preferences>) => {
    const next = { ...prefs, ...patch };
    setPrefs(next);
    localStorage.setItem(PREFERENCES_KEY, JSON.stringify(next));
    applyPersonalPreferences(next);
    setNotice('Preferences saved');
  };

  return (
    <div
      className="settings-stack"
      data-testid="personal-preferences-settings"
      data-preferences-key={PREFERENCES_KEY}
    >
      <InlineStatus notice={notice} />
      <div className="settings-grid-two">
        <label className="settings-field">
          <span>Theme</span>
          <PanelSelect
            data-testid="personal-pref-theme"
            value={prefs.theme}
            onChange={(event) => update({ theme: event.currentTarget.value as Preferences['theme'] })}
          >
            <option value="system">System</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </PanelSelect>
        </label>
      </div>
      <AiCallModeSettings />
    </div>
  );
}

function PersonalPrivacySettings() {
  const [enabled, setEnabled] = useState(telemetryPreference() === 'enabled');
  const { data: config, error, loading } = useLoader<RuntimeConfig>(() => getRuntimeConfig());

  const update = (next: boolean) => {
    setTelemetryPreference(next);
    setEnabled(next);
  };

  return (
    <div className="settings-stack" data-testid="personal-privacy-settings">
      <InlineStatus error={error} loading={loading} />
      <section className="settings-subsection">
        <div className="settings-section-kicker">Product telemetry</div>
        <p className="settings-help">
          Share pseudonymous feature use, outcomes, and coarse size and timing ranges.
          Product telemetry does not include your data, filenames, names, prompts, results,
          URLs, or error messages. The random browser identifier resets monthly, and
          sensitive projects send no project telemetry.
        </p>
        {config?.product_telemetry_available ? (
          <label className="setting-check-row">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(event) => update(event.currentTarget.checked)}
              data-testid="product-telemetry-enabled"
            />
            <span>Send product telemetry</span>
          </label>
        ) : config ? (
          <ReadOnlyNotice>Product telemetry is not configured for this installation.</ReadOnlyNotice>
        ) : null}
      </section>
    </div>
  );
}

interface ProviderFormState {
  adding: boolean;
  provider: string;
  keyValue: string;
  cap: string;
  notice: string | null;
  mutationError: string | null;
  validating: boolean;
  validation: {
    provider: string;
    keyValue: string;
    token: string;
    message: string;
  } | null;
  validationError: string | null;
  validationErrorTone: Exclude<ProviderValidationTone, 'success'>;
}
type ProviderFormAction =
  | { type: 'toggle_add' }
  | { type: 'set_provider'; value: string }
  | { type: 'set_key_value'; value: string }
  | { type: 'set_cap'; value: string }
  | { type: 'validation_start' }
  | { type: 'validation_success'; token: string; message: string }
  | { type: 'validation_error'; error: string; tone?: Exclude<ProviderValidationTone, 'success'> }
  | { type: 'mutation_success'; notice: string }
  | { type: 'mutation_error'; error: string };
function providerFormReducer(state: ProviderFormState, action: ProviderFormAction): ProviderFormState {
  switch (action.type) {
    case 'toggle_add': return { ...state, adding: !state.adding, notice: null, mutationError: null, validation: null, validationError: null };
    case 'set_provider': return { ...state, provider: action.value, validation: null, validationError: null };
    case 'set_key_value': return { ...state, keyValue: action.value, validation: null, validationError: null };
    case 'set_cap': return { ...state, cap: action.value };
    case 'validation_start': return { ...state, validating: true, validation: null, validationError: null, mutationError: null, notice: null };
    case 'validation_success': return {
      ...state,
      validating: false,
      validation: {
        provider: state.provider,
        keyValue: state.keyValue.trim(),
        token: action.token,
        message: action.message,
      },
      validationError: null,
    };
    case 'validation_error': return { ...state, validating: false, validation: null, validationError: action.error, validationErrorTone: action.tone ?? 'error' };
    case 'mutation_success': return { ...providerFormInitial, provider: state.provider, notice: action.notice };
    case 'mutation_error': return { ...state, notice: null, mutationError: action.error };
  }
}
const providerFormInitial: ProviderFormState = {
  adding: false,
  provider: 'anthropic',
  keyValue: '',
  cap: '',
  notice: null,
  mutationError: null,
  validating: false,
  validation: null,
  validationError: null,
  validationErrorTone: 'error',
};

function providerValidationMessage(result: ProviderValidateResult): string {
  if (result.ok) return 'Valid — key accepted';
  if (result.reachable) return result.detail || `Reachable but rejected (HTTP ${result.status ?? '?'})`;
  return `Unreachable${result.detail ? `: ${result.detail}` : ''}`;
}

type ProviderValidationTone = 'success' | 'error' | 'warning';

// A reachable-but-rejected key (e.g. HTTP 401) is a real error the user must
// fix; an unreachable provider is more likely transient/network, so it reads
// as a warning rather than a hard failure.
function providerValidationTone(result: ProviderValidateResult): ProviderValidationTone {
  if (result.ok) return 'success';
  return result.reachable ? 'error' : 'warning';
}

function providerFormCanSave(form: ProviderFormState): boolean {
  return Boolean(
    form.keyValue.trim() &&
    form.validation?.provider === form.provider &&
    form.validation.keyValue === form.keyValue.trim() &&
    form.validation.token,
  );
}

export function OrganizationAiProvidersSettings() {
  const catalog = useLoader<ProviderCatalog>(() => providerCatalog());
  const keys = useLoader<OrgKeyInfo[]>(() => listOrgKeys());
  const [form, dispatch] = useReducer(providerFormReducer, providerFormInitial);
  const [existingTests, setExistingTests] = useState<
    Record<string, { text: string; tone: ProviderValidationTone | 'pending' }>
  >({});

  const rows = keys.data ?? [];
  const canSave = providerFormCanSave(form);
  const testNewKey = async () => {
    if (!form.keyValue.trim()) return;
    dispatch({ type: 'validation_start' });
    try {
      const result = await validateOrgKey(form.provider, form.keyValue.trim());
      if (result.ok && result.validation_token) {
        dispatch({ type: 'validation_success', token: result.validation_token, message: providerValidationMessage(result) });
      } else {
        dispatch({ type: 'validation_error', error: providerValidationMessage(result), tone: providerValidationTone(result) as Exclude<ProviderValidationTone, 'success'> });
      }
    } catch (caught) {
      dispatch({ type: 'validation_error', error: errMsg(caught) });
    }
  };
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
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!canSave || !form.validation) return;
    try {
      await setOrgKey(form.provider, form.keyValue, form.validation.token);
      dispatch({ type: 'mutation_success', notice: 'Provider key saved' });
      await keys.reload();
    } catch (caught) {
      dispatch({ type: 'mutation_error', error: errMsg(caught) });
    }
  };

  return (
    <div className="settings-stack" data-testid="organization-ai-providers-settings">
      <InlineStatus error={catalog.error ?? keys.error ?? form.mutationError} loading={catalog.loading || keys.loading} notice={form.notice} />
      <button className="btn" type="button" onClick={() => dispatch({ type: 'toggle_add' })}>
        <KeyRound size={14} /> Add new key
      </button>
      {form.adding && (
        <form className="settings-form settings-inline-form settings-provider-key-form" autoComplete="off" onSubmit={save}>
          <label>
            <span>Provider</span>
            <PanelSelect
              aria-label="Provider"
              value={form.provider}
              onChange={(event) => dispatch({ type: 'set_provider', value: event.currentTarget.value })}
            >
              {(catalog.data?.providers ?? []).map((item) => (
                <option key={item.id} value={item.id}>{item.label}</option>
              ))}
            </PanelSelect>
          </label>
          <label>
            <span>Key</span>
            <input
              aria-label="Provider key"
              type="password"
              autoComplete="new-password"
              placeholder="Provider key"
              value={form.keyValue}
              onChange={(event) => dispatch({ type: 'set_key_value', value: event.currentTarget.value })}
            />
          </label>
          {/* No spend cap here: org keys are stored by the control plane
              (team/schema.py org_keys), which has no cap or accrual column —
              metered caps against an org key are private commerce and are not
              part of this edition. The input used to be rendered and silently
              discarded by POST /api/org/keys. Project-level provider keys
              (below) DO carry a real, enforced cap. */}
          <button className="btn" type="button" disabled={form.validating || !form.keyValue.trim()} onClick={() => void testNewKey()}>
            Test
          </button>
          <button className="btn btn-primary" type="submit" disabled={form.validating || !canSave}>
            <KeyRound size={14} /> Save
          </button>
          {(form.validation || form.validationError) && (
            <p
              className={`settings-inline-status settings-validation-message is-${form.validation ? 'success' : form.validationErrorTone}`}
              data-testid="provider-validation-message"
              role={form.validationError ? 'alert' : undefined}
            >
              {form.validation?.message ?? form.validationError}
            </p>
          )}
        </form>
      )}
      <SettingsTable
        headers={['Provider', 'Key', '']}
        rows={rows.map((row) => [
          row.provider,
          <>
            <span>{row.hint}</span>
            {existingTests[row.provider] && (
              <div
                className={`settings-row-note${existingTests[row.provider].tone === 'pending' ? '' : ` settings-validation-message is-${existingTests[row.provider].tone}`}`}
                data-testid={`provider-existing-validation-${row.provider}`}
              >
                {existingTests[row.provider].text}
              </div>
            )}
          </>,
          <span key={row.provider} className="settings-row-actions">
            <button
              className="btn btn-compact"
              type="button"
              aria-label={`Test ${row.provider} provider key`}
              onClick={() => void testExisting(row.provider)}
            >
              Test
            </button>
            <button
              className="icon-btn"
              type="button"
              aria-label={`Delete ${row.provider} provider key`}
              title={`Delete ${row.provider} provider key`}
              onClick={() => void deleteOrgKey(row.provider)
                .then(keys.reload)
                .catch((caught) => dispatch({ type: 'mutation_error', error: errMsg(caught) }))}
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
      <div className="settings-inline-error" role="alert" data-testid="org-local-models-fetch-error">
        {info.error}
      </div>
    );
  }
  const endpoint = data.endpoints[0];
  if (!endpoint) return null;

  const status = orgLocalModelsStatusChip(endpoint);
  const models = endpoint.models;

  return (
    <div className="settings-subsection org-local-models" data-testid="org-local-models-settings">
      <p className="settings-meta-label">
        {endpoint.label}{endpoint.origin ? ` — ${endpoint.origin}` : ''}{' '}
        <StatusChip tone={status.tone} testId="org-local-models-status">
          {status.text}
        </StatusChip>
      </p>
      {endpoint.protocol && (
        <p className="settings-help" data-testid="org-local-models-protocol">
          Protocol: {endpoint.protocol}
        </p>
      )}
      {models.length > 0 ? (
        <ul className="org-local-models-list" data-testid="org-local-models-list">
          {models.map((model) => <li key={model.id}>{model.label}</li>)}
        </ul>
      ) : (
        <p className="settings-help" data-testid="org-local-models-empty">No models installed</p>
      )}
      {endpoint.pull_enabled ? (
        <OrgModelPullDownload endpointId={endpoint.endpoint_id} onInstalled={info.reload} />
      ) : (
        <p className="settings-help" data-testid="org-local-models-pull-disabled">
          Model downloads are disabled; set FRISKET_ENABLE_MODEL_PULL on the server.
        </p>
      )}
      <OrgModelPullList />
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
      <div className="settings-subsection org-model-pull-download" data-testid="org-model-pull-download">
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
    <div className="settings-subsection org-model-pull-download" data-testid="org-model-pull-download">
      <label>
        <span>Download a model</span>
        <input
          type="text"
          aria-label="Model name"
          data-testid="org-model-pull-input"
          placeholder="e.g. qwen3:8b"
          value={modelInput}
          disabled={busy || phase === 'confirm'}
          onChange={(event) => setModelInput(event.target.value)}
        />
      </label>
      {phase === 'confirm' ? (
        <div className="settings-row-actions">
          <p className="local-server-guidance-copy">
            This may download several GB to the operator&rsquo;s local server.
          </p>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="org-model-pull-confirm"
            disabled={busy}
            onClick={() => void startDownload()}
          >
            Confirm
          </button>
          <button
            type="button"
            className="btn"
            data-testid="org-model-pull-cancel-confirm"
            disabled={busy}
            onClick={() => setPhase('idle')}
          >
            Cancel
          </button>
        </div>
      ) : (
        <button
          type="button"
          className="btn btn-primary"
          data-testid="org-model-pull-start"
          disabled={!modelInput.trim()}
          onClick={() => setPhase('confirm')}
        >
          <Download size={14} /> Download
        </button>
      )}
      {phase === 'error' && error && (
        <p
          className="settings-inline-status settings-validation-message is-error"
          data-testid="org-model-pull-error"
        >
          {error}
        </p>
      )}
    </div>
  );
}

// Recent/active pulls strip, org variant of ModelPullList below -- same
// independent-fetch-on-mount + refresh-on-ModelPullProgress-onDone pattern,
// pointed at the org routes via ModelPullProgress's injectable fetchPull/
// cancelPull props instead of their workspace-route defaults.
function OrgModelPullList() {
  const [pulls, setPulls] = useState<ModelPullDto[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const result = await orgListModelPulls();
      setPulls(result.pulls);
      setError(null);
    } catch (caught) {
      setError(errMsg(caught));
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const activePulls = pulls.filter((p) => p.status === 'pending' || p.status === 'running');
  const finishedPulls = pulls
    .filter((p) => p.status !== 'pending' && p.status !== 'running')
    .slice(0, 5);

  return (
    <div className="settings-subsection model-pull-list" data-testid="org-model-pull-list">
      {error && (
        <p
          className="settings-inline-status settings-validation-message is-error"
          data-testid="org-model-pull-list-error"
        >
          {error}
        </p>
      )}
      {activePulls.map((pull) => (
        <ModelPullProgress
          key={pull.id}
          pull={pull}
          fetchPull={orgGetModelPull}
          cancelPull={orgCancelModelPull}
          onDone={() => void load()}
        />
      ))}
      {finishedPulls.length > 0 && (
        <ul className="model-pull-history">
          {finishedPulls.map((pull) => (
            <li
              key={pull.id}
              className="model-pull-history-row"
              data-testid={`org-model-pull-history-${pull.id}`}
            >
              <span className="model-pull-history-model">{pull.model}</span>
              <span className="model-pull-history-status">{pull.status}</span>
              <span className="model-pull-history-size">
                {pull.resolved_size != null ? formatNumberDisplay(pull.resolved_size, 'filesize') : '—'}
              </span>
            </li>
          ))}
        </ul>
      )}
      {loaded && !error && activePulls.length === 0 && finishedPulls.length === 0 && (
        <p className="settings-help">No downloads yet.</p>
      )}
    </div>
  );
}

function OrganizationSecretsSettings() {
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
    <div className="settings-stack" data-testid="organization-secrets-settings">
      <InlineStatus error={secrets.error ?? mutationError} loading={secrets.loading} notice={notice} />
      <form className="settings-form settings-inline-form" autoComplete="off" onSubmit={save}>
        <input
          aria-label="Secret name"
          autoComplete="off"
          placeholder="Name"
          value={name}
          onChange={(event) => setName(event.currentTarget.value)}
        />
        <input
          aria-label="Secret value"
          type="password"
          autoComplete="new-password"
          placeholder="Value"
          value={value}
          onChange={(event) => setValue(event.currentTarget.value)}
        />
        <button className="btn btn-primary" type="submit" disabled={!name.trim() || !value}>
          <Save size={14} /> Save
        </button>
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

function OrganizationApiKeysSettings() {
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
    <div className="settings-stack" data-testid="organization-api-keys-settings">
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
            <button className="btn" type="button" onClick={() => void copyCreatedToken()}>
              {copyStatus === 'copied' ? <Check size={14} /> : <Copy size={14} />}
              {copyStatus === 'copied' ? 'Copied' : 'Copy'}
            </button>
          </div>
          {copyStatus === 'failed' && (
            <div className="settings-copy-status">Copy unavailable</div>
          )}
        </output>
      )}
      <form className="settings-form settings-inline-form" onSubmit={save}>
        <input aria-label="Token name" placeholder="Token name" value={name} onChange={(event) => setName(event.currentTarget.value)} />
        <button className="btn btn-primary" type="submit" disabled={!name.trim()}>
          <KeyRound size={14} /> Create
        </button>
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
            <button
              key={row.id}
              className="btn"
              type="button"
              onClick={() => void revokeApiKey(row.id)
                .then(tokens.reload)
                .catch((caught) => setMutationError(errMsg(caught)))}
            >
              Revoke
            </button>
          ),
        ])}
        empty="No API keys"
      />
    </div>
  );
}

function OrganizationSpendSettings() {
  const spend = useLoader<SpendReport>(() => getSpend());
  return (
    <div className="settings-stack" data-testid="organization-spend-settings">
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

interface ProjectGeneralFormState {
  name: string | null;
  description: string | null;
  confirmDelete: string;
  notice: string | null;
  mutationError: string | null;
  busy: boolean;
}
type ProjectGeneralFormAction =
  | { type: 'set_name'; value: string }
  | { type: 'set_description'; value: string }
  | { type: 'set_confirm_delete'; value: string }
  | { type: 'save_start' }
  | { type: 'save_success'; notice: string }
  | { type: 'save_error'; error: string }
  | { type: 'delete_start' }
  | { type: 'delete_error'; error: string };
function projectGeneralReducer(state: ProjectGeneralFormState, action: ProjectGeneralFormAction): ProjectGeneralFormState {
  switch (action.type) {
    case 'set_name': return { ...state, name: action.value };
    case 'set_description': return { ...state, description: action.value };
    case 'set_confirm_delete': return { ...state, confirmDelete: action.value };
    case 'save_start': return { ...state, busy: true, mutationError: null };
    case 'save_success': return { ...state, busy: false, notice: action.notice };
    case 'save_error': return { ...state, busy: false, notice: null, mutationError: action.error };
    case 'delete_start': return { ...state, busy: true, mutationError: null };
    case 'delete_error': return { ...state, busy: false, mutationError: action.error };
  }
}
const projectGeneralInitial: ProjectGeneralFormState = {
  name: null, description: null, confirmDelete: '', notice: null, mutationError: null, busy: false,
};

export function ProjectGeneralSettings({ project }: { project?: ProjectInfo | null }) {
  const { projectApi: api } = useWorkspaceStores();
  const details = useLoader<ProjectInfo>(() => api.getProject(), project?.id ?? '');
  const [form, dispatch] = useReducer(projectGeneralReducer, projectGeneralInitial);
  const nameValue = form.name ?? details.data?.name ?? project?.name ?? '';
  const descriptionValue = form.description ?? details.data?.description ?? project?.description ?? '';
  const current = details.data ?? project;
  const canEdit = canEditProject(current);
  const canOwner = canOwnProject(current);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!canEdit) return;
    dispatch({ type: 'save_start' });
    try {
      const saved = await api.updateCurrentProject({
        name: nameValue,
        description: descriptionValue,
      });
      details.setData(saved);
      dispatch({ type: 'save_success', notice: 'Project saved' });
    } catch (caught) {
      dispatch({ type: 'save_error', error: errMsg(caught) });
    }
  };

  return (
    <div className="settings-stack" data-testid="project-general-settings">
      <InlineStatus error={details.error ?? form.mutationError} loading={details.loading} notice={form.notice} />
      {!canEdit && (
        <ReadOnlyNotice>
          Your project role can view project identity but cannot change it.
        </ReadOnlyNotice>
      )}
      <form className="settings-form" onSubmit={save}>
        <label>
          <span>Name</span>
          <input
            data-testid="project-name-input"
            value={nameValue}
            disabled={!canEdit || form.busy}
            onChange={(event) => dispatch({ type: 'set_name', value: event.currentTarget.value })}
          />
        </label>
        <label>
          <span>Description</span>
          <textarea
            data-testid="project-description-input"
            value={descriptionValue}
            disabled={!canEdit || form.busy}
            onChange={(event) => dispatch({ type: 'set_description', value: event.currentTarget.value })}
          />
        </label>
        <div className="settings-meta-list">
          <div><dt>Project</dt><dd>{current?.name ?? '-'}</dd></div>
          <div><dt>Project ID</dt><dd>{current?.id ?? '-'}</dd></div>
          <div><dt>Sensitive</dt><dd>{current?.sensitive ? 'Yes' : 'No'}</dd></div>
        </div>
        <div className="settings-actions">
          <button className="btn btn-primary" type="submit" disabled={!canEdit || form.busy}>
            <Save size={14} /> Save
          </button>
        </div>
      </form>
      {!canOwner && (
        <ReadOnlyNotice>
          Delete project is restricted to project owners.
        </ReadOnlyNotice>
      )}
      <form className="settings-danger-zone" onSubmit={(event) => {
        event.preventDefault();
        if (form.confirmDelete === current?.name && canOwner) {
          dispatch({ type: 'delete_start' });
          void api.deleteProject(current?.id, form.confirmDelete)
            .then(() => navigate({ kind: 'picker' }))
            .catch((caught) => dispatch({
              type: 'delete_error',
              error: caught instanceof ApiError && caught.status === 409
                ? 'This project still has runs in flight. Wait for them to finish or cancel them, then delete.'
                : errMsg(caught),
            }));
        }
      }}>
        <div className="settings-danger-header">
          <AlertTriangle size={16} />
          <div>
            <h2>Danger zone</h2>
            <p>Deleting this project permanently removes its sheets, files, run history, and settings. This cannot be undone.</p>
          </div>
        </div>
        <label>
          <span>Type {current?.name ?? 'the project name'} to delete</span>
          <ConfirmTypeInput
            data-testid="project-delete-confirm-input"
            id="frisket-project-delete-gate"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
            value={form.confirmDelete}
            disabled={!canOwner || form.busy}
            onChange={(value) => dispatch({ type: 'set_confirm_delete', value })}
          />
        </label>
        <button
          className="btn btn-reject settings-danger-button"
          data-testid="project-delete-button"
          type="submit"
          disabled={!canOwner || form.busy || form.confirmDelete !== current?.name}
        >
          <Trash2 size={14} /> Delete
        </button>
      </form>
    </div>
  );
}

interface ProjectAccessState {
  members: ProjectMember[] | null;
  invites: ProjectInvite[] | null;
  email: string;
  role: ProjectInviteRole;
  memberError: string | null;
  inviteError: string | null;
  actionError: string | null;
  notice: string | null;
  busy: string | null;
  freshAuthRequired: boolean;
  loadedProjectId: string | null;
}
type ProjectAccessAction =
  | { type: 'load_result'; members: ProjectMember[]; invites: ProjectInvite[]; memberError: string | null; inviteError: string | null; loadedProjectId: string }
  | { type: 'load_error'; error: string }
  | { type: 'action_start'; label: string }
  | { type: 'action_success'; notice: string }
  | { type: 'action_error'; error: string; freshAuthRequired: boolean }
  | { type: 'set_email'; value: string }
  | { type: 'set_role'; value: ProjectInviteRole }
  | { type: 'clear_email' };
function projectAccessReducer(state: ProjectAccessState, action: ProjectAccessAction): ProjectAccessState {
  switch (action.type) {
    case 'load_result':
      return { ...state, members: action.members, invites: action.invites, memberError: action.memberError, inviteError: action.inviteError, loadedProjectId: action.loadedProjectId };
    case 'load_error':
      return { ...state, actionError: action.error };
    case 'action_start':
      return { ...state, busy: action.label, actionError: null, notice: null, freshAuthRequired: false };
    case 'action_success':
      return { ...state, busy: null, notice: action.notice };
    case 'action_error':
      return { ...state, busy: null, actionError: action.error, freshAuthRequired: action.freshAuthRequired };
    case 'set_email':
      return { ...state, email: action.value };
    case 'set_role':
      return { ...state, role: action.value };
    case 'clear_email':
      return { ...state, email: '' };
  }
}
const projectAccessInitial: ProjectAccessState = {
  members: null, invites: null, email: '', role: 'viewer',
  memberError: null, inviteError: null, actionError: null, notice: null,
  busy: null, freshAuthRequired: false, loadedProjectId: null,
};

function ProjectAccessSettings({ project, identityMode }: { project?: ProjectInfo | null; identityMode: boolean }) {
  const [state, dispatch] = useReducer(projectAccessReducer, projectAccessInitial);
  const projectId = project?.id ?? '';
  const canManageProject = ['owner', 'editor'].includes(projectRole(project));
  const canManageOwners = canOwnProject(project);
  const assignableRoles = canManageOwners
    ? MEMBER_ROLES
    : MEMBER_ROLES.filter((item) => item !== 'owner');
  const load = useCallback(async () => {
    if (!projectId || !identityMode) return;
    const [memberResult, inviteResult] = await Promise.allSettled([
      listProjectMembers(projectId),
      listProjectInvites(projectId),
    ]);
    dispatch({
      type: 'load_result',
      members: memberResult.status === 'fulfilled' ? memberResult.value : [],
      invites: inviteResult.status === 'fulfilled' ? inviteResult.value : [],
      memberError: memberResult.status === 'rejected'
        ? accessErrorMessage(memberResult.reason, 'You do not have permission to view project members.')
        : null,
      inviteError: inviteResult.status === 'rejected'
        ? accessErrorMessage(inviteResult.reason, 'You do not have permission to manage project invites.')
        : null,
      loadedProjectId: projectId,
    });
  }, [identityMode, projectId]);
  useEffect(() => {
    if (!identityMode) return undefined;
    let alive = true;
    void Promise.resolve().then(async () => {
      try {
        await load();
      } catch (caught) {
        if (alive) dispatch({ type: 'load_error', error: errMsg(caught) });
      }
    });
    return () => {
      alive = false;
    };
  }, [identityMode, load]);
  if (!identityMode) {
    return <div className="settings-empty-state">Project access is available on hosted workspaces.</div>;
  }
  const loading = state.loadedProjectId !== projectId;
  const runAction = async (label: string, action: () => Promise<void>) => {
    if (state.busy !== null || !canManageProject) return;
    dispatch({ type: 'action_start', label });
    try {
      await action();
      dispatch({ type: 'action_success', notice: label });
    } catch (caught) {
      dispatch({
        type: 'action_error',
        error: isFreshAuthError(caught)
          ? 'Sign in again to change project access.'
          : accessErrorMessage(caught, 'You do not have permission to manage project access.'),
        freshAuthRequired: isFreshAuthError(caught),
      });
    }
  };
  return (
    <div className="settings-stack" data-testid="project-access-settings">
      <InlineStatus error={state.actionError} loading={loading} notice={state.notice} />
      {state.freshAuthRequired && (
        <button
          className="btn"
          type="button"
          data-testid="project-access-fresh-auth"
          onClick={() => void signOut().finally(() => window.location.assign('/'))}
        >
          Sign in again
        </button>
      )}
      {!canManageProject && (
        <ReadOnlyNotice>
          Your project role can view access state but cannot change members or invites.
        </ReadOnlyNotice>
      )}
      {canManageProject && (
        <form className="settings-form settings-inline-form" onSubmit={(event) => {
          event.preventDefault();
          const inviteEmail = state.email.trim();
          if (!inviteEmail) return;
          void runAction('Invite created', async () => {
            await createProjectInvite(projectId, inviteEmail, state.role);
            dispatch({ type: 'clear_email' });
            await load();
          });
        }}>
          <input
            aria-label="Invite email"
            data-testid="project-invite-email"
            placeholder="Email"
            value={state.email}
            disabled={state.busy !== null}
            onChange={(event) => dispatch({ type: 'set_email', value: event.currentTarget.value })}
          />
          <PanelSelect
            data-testid="project-invite-role"
            value={state.role}
            disabled={state.busy !== null}
            onChange={(event) => dispatch({ type: 'set_role', value: event.currentTarget.value as ProjectInviteRole })}
          >
            {INVITE_ROLES.map((item) => <option key={item} value={item}>{item}</option>)}
          </PanelSelect>
          <button className="btn btn-primary" data-testid="project-invite-submit" type="submit" disabled={state.busy !== null || !state.email.trim()}>Invite</button>
        </form>
      )}
      <InlineStatus error={state.memberError} />
      <SettingsTable
        headers={['Member', 'Role', '']}
        rows={(state.members ?? []).map((member) => [
          member.email,
          <PanelSelect
            key={`${memberKey(member)}-role`}
            data-testid={`project-member-role-${memberKey(member)}`}
            value={member.role}
            disabled={
              state.busy !== null ||
              !canManageProject ||
              (member.role === 'owner' && !canManageOwners)
            }
            onChange={(event) => void runAction('Role updated', async () => {
              await setProjectMember(projectId, member.email, event.currentTarget.value as ProjectRole);
              await load();
            })}
          >
            {assignableRoles.includes(member.role)
              ? null
              : <option key={member.role} value={member.role}>{member.role}</option>}
            {assignableRoles.map((item) => <option key={item} value={item}>{item}</option>)}
          </PanelSelect>,
          <button
            key={`${memberKey(member)}-remove`}
            className="icon-btn"
            data-testid={`project-member-remove-${memberKey(member)}`}
            type="button"
            aria-label={`Remove ${member.email}`}
            title={`Remove ${member.email}`}
            disabled={
              state.busy !== null ||
              !canManageProject ||
              (member.role === 'owner' && !canManageOwners)
            }
            onClick={() => void runAction('Member removed', async () => {
              await removeProjectMember(projectId, member.email);
              await load();
            })}
          >
            <Trash2 size={14} />
          </button>,
        ])}
        empty="No members"
      />
      <InlineStatus error={state.inviteError} />
      <SettingsTable
        headers={['Pending invite', 'Role', 'Expires', '']}
        rows={(state.invites ?? []).map((invite) => [
          invite.email,
          invite.role,
          formatDate(invite.expires_at),
          <button
            key={invite.id}
            className="btn"
            data-testid={`project-invite-revoke-${invite.id}`}
            type="button"
            disabled={state.busy !== null || !canManageProject}
            onClick={() => void runAction('Invite revoked', async () => {
              await revokeProjectInvite(projectId, invite.id);
              await load();
            })}
          >
            Revoke
          </button>,
        ])}
        empty="No pending invites"
      />
    </div>
  );
}

// Exported for component tests, same precedent as the two AI-providers
// sections above: the money cells (cap, spent, unset) are the surface the
// sub-cent formatter exists for, and asserting them through the full
// SettingsSectionRenderer would test the router instead.
export function ProjectAiProvidersSettings({ project }: { project?: ProjectInfo | null }) {
  const { projectApi: api } = useWorkspaceStores();
  const keys = useLoader<ProjectProviderKeys>(() => api.getProjectProviderKeys());
  const [form, dispatch] = useReducer(providerFormReducer, providerFormInitial);
  const [existingTests, setExistingTests] = useState<
    Record<string, { text: string; tone: ProviderValidationTone | 'pending' }>
  >({});
  const canMutate = canOwnProject(project);
  const canSave = canMutate && providerFormCanSave(form);
  const testNewKey = async () => {
    if (!canMutate || !form.keyValue.trim()) return;
    dispatch({ type: 'validation_start' });
    try {
      const result = await api.validateProjectProviderKey(form.provider, form.keyValue.trim());
      if (result.ok && result.validation_token) {
        dispatch({ type: 'validation_success', token: result.validation_token, message: providerValidationMessage(result) });
      } else {
        dispatch({ type: 'validation_error', error: providerValidationMessage(result), tone: providerValidationTone(result) as Exclude<ProviderValidationTone, 'success'> });
      }
    } catch (caught) {
      dispatch({ type: 'validation_error', error: errMsg(caught) });
    }
  };
  const testExisting = async (provider: string, label: string) => {
    if (!canMutate) return;
    setExistingTests((current) => ({ ...current, [provider]: { text: 'Testing...', tone: 'pending' } }));
    try {
      const result = await api.validateProjectProviderKey(provider);
      setExistingTests((current) => ({
        ...current,
        [provider]: { text: providerValidationMessage(result), tone: providerValidationTone(result) },
      }));
    } catch (caught) {
      setExistingTests((current) => ({ ...current, [provider]: { text: errMsg(caught) || `Could not test ${label}`, tone: 'error' } }));
    }
  };
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!canSave || !form.validation) return;
    try {
      keys.setData(await api.setProjectProviderKey(
        form.provider,
        form.keyValue,
        form.cap ? Number(form.cap) : null,
        form.validation.token,
      ));
      dispatch({ type: 'mutation_success', notice: 'Provider override saved' });
    } catch (caught) {
      dispatch({ type: 'mutation_error', error: errMsg(caught) });
    }
  };
  return (
    <div className="settings-stack" data-testid="project-ai-providers-settings">
      <InlineStatus error={keys.error ?? form.mutationError} loading={keys.loading} notice={form.notice} />
      {!canMutate && (
        <ReadOnlyNotice>
          Your project role can view provider override status but cannot change provider keys.
        </ReadOnlyNotice>
      )}
      <button className="btn" type="button" disabled={!canMutate} onClick={() => dispatch({ type: 'toggle_add' })}>
        <KeyRound size={14} /> Add new key
      </button>
      {form.adding && (
        <form className="settings-form settings-inline-form settings-provider-key-form" autoComplete="off" onSubmit={save}>
          <label>
            <span>Provider</span>
            <PanelSelect
              aria-label="Provider"
              value={form.provider}
              disabled={!canMutate}
              onChange={(event) => dispatch({ type: 'set_provider', value: event.currentTarget.value })}
            >
              {(keys.data?.providers ?? []).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </PanelSelect>
          </label>
          <label>
            <span>Key</span>
            <input
              aria-label="Project provider key"
              type="password"
              autoComplete="new-password"
              placeholder="Provider key"
              value={form.keyValue}
              disabled={!canMutate}
              onChange={(event) => dispatch({ type: 'set_key_value', value: event.currentTarget.value })}
            />
          </label>
          {/* The write side used to state no unit at all while the read side
              (the Cap column) rendered the same number as currency — so "10"
              could plausibly be dollars, cents, or calls. The unit is named
              here, and blank is spelled out as "no cap" rather than left to
              be inferred from an empty box. */}
          <label>
            <span>Spend cap (USD)</span>
            <input
              aria-label="Spend cap in US dollars"
              inputMode="decimal"
              placeholder="e.g. 5.00 — blank for no cap"
              value={form.cap}
              disabled={!canMutate}
              onChange={(event) => dispatch({ type: 'set_cap', value: event.currentTarget.value })}
            />
          </label>
          <button className="btn" type="button" disabled={!canMutate || form.validating || !form.keyValue.trim()} onClick={() => void testNewKey()}>
            Test
          </button>
          <button className="btn btn-primary" type="submit" disabled={form.validating || !canSave}><KeyRound size={14} /> Save</button>
          {(form.validation || form.validationError) && (
            <p
              className={`settings-inline-status settings-validation-message is-${form.validation ? 'success' : form.validationErrorTone}`}
              data-testid="provider-validation-message"
              role={form.validationError ? 'alert' : undefined}
            >
              {form.validation?.message ?? form.validationError}
            </p>
          )}
        </form>
      )}
      <SettingsTable
        headers={['Provider', 'Secret', 'Key', 'Cap', 'Spent', '']}
        rows={(keys.data?.providers ?? []).map((row) => [
          row.label,
          row.secret_name,
          <>
            <span>{row.configured ? row.hint : '-'}</span>
            {existingTests[row.id] && (
              <div
                className={`settings-row-note${existingTests[row.id].tone === 'pending' ? '' : ` settings-validation-message is-${existingTests[row.id].tone}`}`}
                data-testid={`provider-existing-validation-${row.id}`}
              >
                {existingTests[row.id].text}
              </div>
            )}
          </>,
          formatUsdOrNone(row.spend_cap_usd),
          <SpentCell key={`${row.id}-spent`} row={row} />,
          row.configured ? (
            <span key={row.id} className="settings-row-actions">
              <button
                className="btn btn-compact"
                type="button"
                aria-label={`Test ${row.label} provider override`}
                disabled={!canMutate}
                onClick={() => void testExisting(row.id, row.label)}
              >
                Test
              </button>
              <button
                className="icon-btn"
                type="button"
                aria-label={`Delete ${row.label} provider override`}
                title={`Delete ${row.label} provider override`}
                disabled={!canMutate}
                onClick={() => void api.deleteProjectProviderKey(row.id)
                  .then(keys.reload)
                  .catch((caught) => dispatch({ type: 'mutation_error', error: errMsg(caught) }))}
              >
                <Trash2 size={14} />
              </button>
            </span>
          ) : '',
        ])}
        empty="No provider catalog"
      />
    </div>
  );
}

// The workspace-level provider page: deliberately the SAME interface as
// ProjectAiProvidersSettings above — the
// shared providerFormReducer add-key flow (validate before save) and a
// SettingsTable of provider rows with Test/Delete — but backed by the
// workspace catalog (/api/providers → <ws>/.frisket/provider_keys.json),
// plus the canonical local endpoint list. Exported for component tests.
export function WorkspaceAiProvidersSettings() {
  const catalog = useLoader<LocalProviderCatalog>(() => listProviders());
  const [form, dispatch] = useReducer(providerFormReducer, providerFormInitial);
  const [existingTests, setExistingTests] = useState<
    Record<string, { text: string; tone: ProviderValidationTone | 'pending' }>
  >({});
  const canSave = providerFormCanSave(form);
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

  const testNewKey = async () => {
    if (!form.keyValue.trim()) return;
    dispatch({ type: 'validation_start' });
    try {
      const result = await validateProviderKey(form.provider, form.keyValue.trim());
      if (result.ok && result.validation_token) {
        dispatch({ type: 'validation_success', token: result.validation_token, message: providerValidationMessage(result) });
      } else {
        dispatch({ type: 'validation_error', error: providerValidationMessage(result), tone: providerValidationTone(result) as Exclude<ProviderValidationTone, 'success'> });
      }
    } catch (caught) {
      dispatch({ type: 'validation_error', error: errMsg(caught) });
    }
  };
  const testExisting = async (provider: string, label: string) => {
    setExistingTests((current) => ({ ...current, [provider]: { text: 'Testing...', tone: 'pending' } }));
    try {
      const result = await validateProviderKey(provider);
      setExistingTests((current) => ({
        ...current,
        [provider]: { text: providerValidationMessage(result), tone: providerValidationTone(result) },
      }));
    } catch (caught) {
      setExistingTests((current) => ({ ...current, [provider]: { text: errMsg(caught) || `Could not test ${label}`, tone: 'error' } }));
    }
  };
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!canSave || !form.validation) return;
    try {
      catalog.setData(await setProviderKey(form.provider, form.keyValue.trim(), form.validation.token));
      dispatch({ type: 'mutation_success', notice: 'Provider key saved' });
    } catch (caught) {
      dispatch({ type: 'mutation_error', error: errMsg(caught) });
    }
  };
  const sourceLabel = (
    row: Extract<LocalProviderEntry, { kind: 'platform_api' }>,
  ): string =>
    row.source === 'env' ? 'Environment' : row.source === 'local_file' ? 'Workspace file' : '—';

  return (
    <div className="settings-stack" data-testid="workspace-ai-providers-settings">
      <InlineStatus error={catalog.error ?? form.mutationError} loading={catalog.loading} notice={form.notice} />
      <p className="settings-help">
        Keys are stored in this workspace only and never sent back to the browser.
        A shell-exported key always wins over one entered here.
      </p>
      <button className="btn" type="button" onClick={() => dispatch({ type: 'toggle_add' })}>
        <KeyRound size={14} /> Add new key
      </button>
      {form.adding && (
        <form className="settings-form settings-inline-form settings-provider-key-form" autoComplete="off" onSubmit={save}>
          <label>
            <span>Provider</span>
            <PanelSelect
              aria-label="Provider"
              value={form.provider}
              onChange={(event) => dispatch({ type: 'set_provider', value: event.currentTarget.value })}
            >
              {keyProviders.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </PanelSelect>
          </label>
          <label>
            <span>Key</span>
            <input
              aria-label="Provider key"
              type="password"
              autoComplete="new-password"
              placeholder="Provider key"
              value={form.keyValue}
              onChange={(event) => dispatch({ type: 'set_key_value', value: event.currentTarget.value })}
            />
          </label>
          <button className="btn" type="button" disabled={form.validating || !form.keyValue.trim()} onClick={() => void testNewKey()}>
            Test
          </button>
          <button className="btn btn-primary" type="submit" disabled={form.validating || !canSave}><KeyRound size={14} /> Save</button>
          {(form.validation || form.validationError) && (
            <p
              className={`settings-inline-status settings-validation-message is-${form.validation ? 'success' : form.validationErrorTone}`}
              data-testid="provider-validation-message"
              role={form.validationError ? 'alert' : undefined}
            >
              {form.validation?.message ?? form.validationError}
            </p>
          )}
        </form>
      )}
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
                className={`settings-row-note${existingTests[row.id].tone === 'pending' ? '' : ` settings-validation-message is-${existingTests[row.id].tone}`}`}
                data-testid={`provider-test-result-${row.id}`}
              >
                {existingTests[row.id].text}
              </div>
            )}
          </>,
          row.configured ? (
            <span key={row.id} className="settings-row-actions">
              <button
                className="btn btn-compact"
                type="button"
                data-testid={`provider-key-test-${row.id}`}
                aria-label={`Test ${row.label} provider key`}
                onClick={() => void testExisting(row.id, row.label)}
              >
                Test
              </button>
              {row.source !== 'env' && (
                <button
                  className="icon-btn"
                  type="button"
                  data-testid={`provider-key-delete-${row.id}`}
                  aria-label={`Delete ${row.label} provider key`}
                  title={`Delete ${row.label} provider key`}
                  onClick={() => void deleteProviderKey(row.id)
                    .then(catalog.reload)
                    .catch((caught) => dispatch({ type: 'mutation_error', error: errMsg(caught) }))}
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
    </div>
  );
}

// Diagnostics section: the self-probe GET /api/diagnose canonical home,
// extracted from the retired DiagnosePanel.tsx <dialog>. Both the error-toast
// "Open Diagnose" link and the action panel's engine-availability disclosure
// now land here via chromeStore's openDiagnosePanel()
// (App.tsx / state/chromeStore.ts), which navigates to this section instead of
// opening a modal.
//
// core/info are typed `unknown`, not a shape, deliberately: the response is
// only type-ASSERTED by fetchDiagnostics below, never runtime-validated
// (src/frisket/operability/diagnostics.py's INFO wrapper returns fn() without validating
// its shape either), so a probe that diverges from the assumed shape must
// degrade gracefully here instead of throwing into the root error boundary
// (entries/mount.tsx).
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Object.entries over a value that MUST be a plain object to iterate safely
 *  (Object.entries itself throws on null/undefined, and silently no-ops on
 *  other primitives) — an absent/null/malformed core or info collection
 *  renders as an empty list instead of crashing. */
function safeEntries(value: unknown): Array<[string, unknown]> {
  return isRecord(value) ? Object.entries(value) : [];
}

function diagnosticsCoreLine(value: unknown): string {
  if (!isRecord(value)) return value == null ? '—' : JSON.stringify(value);
  if (value.ok) return typeof value.detail === 'string' ? value.detail : 'ok';
  return typeof value.error === 'string' ? value.error : 'failed';
}

function diagnosticsInfoLine(value: unknown): string {
  if (isRecord(value)) {
    return typeof value.summary === 'string' ? value.summary : JSON.stringify(value);
  }
  return value == null ? '—' : String(value);
}

type DiagnosticRowStatus = 'ok' | 'failed' | 'info' | 'unknown';

function diagnosticsCoreStatus(value: unknown): DiagnosticRowStatus {
  if (!isRecord(value)) return 'unknown';
  if (value.ok === true) return 'ok';
  if (value.ok === false) return 'failed';
  return 'unknown';
}

function diagnosticLabel(key: string, info = false): string {
  const raw = info ? DIAGNOSTICS_INFO_LABELS[key] : undefined;
  const label = raw ?? key.replace(/_/g, ' ');
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function DiagnosticStatus({ status }: { status: DiagnosticRowStatus }) {
  const copy = status === 'ok'
    ? 'OK'
    : status === 'failed'
      ? 'Failed'
      : status === 'info'
        ? 'Info'
        : 'Unknown';
  const Icon = status === 'ok'
    ? CheckCircle2
    : status === 'failed'
      ? XCircle
      : Info;
  return (
    <span className={`diagnose-status diagnose-status-${status}`} data-status={status}>
      <Icon size={13} aria-hidden />
      {copy}
    </span>
  );
}

const DIAGNOSTICS_INFO_LABELS: Record<string, string> = {
  model_providers: 'Model providers',
  local_model_endpoints: 'Local model endpoints',
  local_engines: 'Local engines',
  models_sidecar: 'Models sidecar',
  media_toolbelt: 'Media toolbelt',
  static_assets: 'Static assets',
  // Plugin HEALTH (counts), as opposed to individual load/runtime FAILURES,
  // which route into the workbench Errors dock instead (dockJobSummary.ts's
  // derivePluginErrorJobs). Confirmed shape: an INFO entry keyed "plugins" of
  // exactly `{available, installed, active, failed, failed_names, summary}`,
  // `summary` a display string, always present in both success and
  // failure/exception cases.
  // diagnosticsInfoLine reads `summary` when present but degrades gracefully
  // (isRecord/JSON.stringify/'—') for any other shape.
  plugins: 'Plugin runtime health',
};

/** Self-probe fetch, hoisted to module scope so the network call lives in the
 *  data layer rather than inside an effect (react-doctor no-fetch-in-effect;
 *  matches useLoader's own load-fn-at-module-scope convention).
 *
 *  `/api/diagnose` accepts an OPTIONAL `project_id` query param
 *  (src/frisket/server/routes/diagnose.py)
 *  that populates the plugin-runtime-health probe for that project. This
 *  section has no project prop of its own, so it reads the SAME
 *  settingsProjectContext.ts store the back-link uses (written by
 *  chromeStore's openDiagnosePanel / AccountMenu's goPersonal before
 *  navigating here) — when a project is in context its id rides along;
 *  otherwise the probe degrades to "no project in scope", same as omitting
 *  the param entirely. */
function loadDiagnostics(): Promise<DiagnosticsReport> {
  const projectId = readSettingsProjectContext()?.id;
  // The project rides on the PATH, never a query parameter: the server's role
  // ladder only resolves a project from {pid}, so a project named anywhere else
  // is invisible to the gate (diagnose-project-on-the-path-v1).
  return fetchDiagnostics(projectId);
}

/** Reachable from LLM-run error states: the same self-probe `frisket doctor`
 *  prints to a terminal, in-app. See src/frisket/operability/diagnostics.py — this section
 *  and the CLI compute identical facts from one shared module. Exported for
 *  component tests, mirroring WorkspaceAiProvidersSettings' own precedent. */
export function DiagnosticsSettings() {
  const report = useLoader<DiagnosticsReport>(() => loadDiagnostics());
  // useLoader's 'fail' action keeps the previous
  // `data` around (so a transient refresh failure doesn't blank out an
  // otherwise-fine report) — but rendering that stale report under an
  // unqualified "Healthy" status, with only a separate error banner above it,
  // reads as "this is the CURRENT state" when it's actually last-known-good.
  // A failed refresh with no prior report at all still degrades to the bare
  // InlineStatus error banner (report.data is null, the block below doesn't
  // render) — this only relabels the case where stale data is ALSO on screen.
  const stale = Boolean(report.error) && !report.loading && report.data != null;
  return (
    <div className="settings-stack" data-testid="settings-diagnostics">
      <InlineStatus error={report.error} loading={report.loading} />
      {report.data && (
        <div className="diagnose-panel-body" data-testid="diagnose-panel-body">
          {stale && (
            <p className="settings-inline-status" data-testid="diagnose-panel-stale-note">
              Refresh failed — showing the last successful check.
            </p>
          )}
          <div className="diagnose-panel-status" data-testid="diagnose-panel-status">
            {stale ? 'Last known: ' : ''}{report.data.healthy ? 'Healthy' : 'Unhealthy'}
          </div>
          <div className="diagnose-panel-table-wrap">
            <table className="diagnose-panel-table" data-testid="diagnose-panel-table">
              <thead>
                <tr>
                  <th scope="col">Check</th>
                  <th scope="col">Status</th>
                  <th scope="col">Detail</th>
                </tr>
              </thead>
              <tbody>
                {safeEntries(report.data.core).map(([key, value]) => (
                  <tr
                    key={`core:${key}`}
                    className={`diagnose-row diagnose-row-${diagnosticsCoreStatus(value)}`}
                    data-testid={`diagnose-row-${key}`}
                  >
                    <th scope="row">{diagnosticLabel(key)}</th>
                    <td><DiagnosticStatus status={diagnosticsCoreStatus(value)} /></td>
                    <td>{diagnosticsCoreLine(value)}</td>
                  </tr>
                ))}
                {safeEntries(report.data.info).map(([key, value]) => (
                  <tr
                    key={`info:${key}`}
                    className="diagnose-row diagnose-row-info"
                    data-testid={`diagnose-row-${key}`}
                  >
                    <th scope="row">{diagnosticLabel(key, true)}</th>
                    <td><DiagnosticStatus status="info" /></td>
                    <td>{diagnosticsInfoLine(value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      <div className="settings-actions">
        <button
          className="btn"
          type="button"
          data-testid="settings-diagnostics-refresh"
          disabled={report.loading}
          onClick={() => void report.reload()}
        >
          <RefreshCw size={14} /> Refresh
        </button>
      </div>
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
    <div className="settings-subsection" data-testid="local-endpoints">
      <div className="settings-section-kicker">Local model servers</div>
      <p className="settings-help">
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
        <div className="settings-actions" data-testid="local-endpoint-suggestions">
          {suggestions.map((suggestion) => (
            <button
              className="btn"
              type="button"
              key={suggestion.origin}
              disabled={busy}
              onClick={() => void addSuggestion(suggestion.label, suggestion.origin)}
            >
              Add {suggestion.label}
            </button>
          ))}
        </div>
      )}
      <button
        className="btn"
        type="button"
        data-testid="local-endpoint-discover"
        disabled={busy}
        onClick={() => void discover()}
      >
        <RefreshCw size={14} /> Find local servers
      </button>
      {!adding ? (
        <button className="btn" type="button" data-testid="local-endpoint-add" disabled={busy} onClick={() => setAdding(true)}>
          Add local server
        </button>
      ) : (
        <form className="settings-form settings-inline-form" data-testid="local-endpoint-add-form" onSubmit={add}>
          <label>
            <span>Name</span>
            <input
              data-testid="local-endpoint-add-name"
              aria-label="Local server name"
              value={name}
              disabled={busy}
              placeholder="LM Studio — gaming PC"
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label>
            <span>URL</span>
            <input
              data-testid="local-endpoint-add-origin"
              type="url"
              aria-label="Local server URL"
              value={url}
              disabled={busy}
              placeholder="http://localhost:1234"
              onChange={(event) => setUrl(event.target.value)}
            />
          </label>
          <button
            className="btn btn-primary"
            type="submit"
            data-testid="local-endpoint-add-submit"
            disabled={busy || !name.trim() || !url.trim()}
          >
            Add server
          </button>
          <button className="btn" type="button" disabled={busy} onClick={() => setAdding(false)}>
            Cancel
          </button>
        </form>
      )}
      {notice && (
        <p className="settings-validation-message is-success" data-testid="local-endpoint-discovery-summary" role="status">
          {notice}
        </p>
      )}
      {error && <p className="settings-validation-message is-error">{error}</p>}
      <ModelPullList />
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
      className="settings-subsection"
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
      <p className="settings-help">
        This URL is the endpoint's immutable identity. To move it to another
        URL, add a new server and then delete this one.
        {endpoint.read_only && ' This endpoint is managed by the server environment and cannot be edited here.'}
      </p>
      <LocalEndpointProbeDetail endpoint={endpoint} />
      <form className="settings-form settings-inline-form" onSubmit={save}>
        <label>
          <span>Name</span>
          <input data-testid={`local-endpoint-name-${endpointId}`} value={name} disabled={busy || endpoint.read_only} onChange={(event) => setName(event.target.value)} />
        </label>
        <label>
          <span>URL</span>
          <input data-testid={`local-endpoint-origin-${endpointId}`} type="url" value={url} disabled />
        </label>
        {!endpoint.read_only && (
          <button className="btn" type="submit" disabled={busy || !dirty || !name.trim()}>
            <Save size={14} /> Save
          </button>
        )}
        <button className="btn" type="button" disabled={busy} onClick={() => void onRecheck()}>
          <RefreshCw size={14} /> Recheck
        </button>
        {!endpoint.read_only && (
          <button className="icon-btn" type="button" disabled={busy} aria-label={`Delete ${endpoint.label}`} onClick={() => void remove()}>
            <Trash2 size={14} />
          </button>
        )}
      </form>
      {!endpoint.read_only && (
        <>
          <form className="settings-form settings-inline-form" data-testid={`local-endpoint-auth-${endpointId}`} onSubmit={saveCredentials}>
            <label>
              <span>Inference token {endpoint.token_configured ? '(configured)' : ''}</span>
              <input
                type="password"
                value={inferenceToken}
                autoComplete="new-password"
                placeholder="Leave blank to keep current token"
                disabled={busy}
                onChange={(event) => setInferenceToken(event.target.value)}
              />
            </label>
            <label>
              <span>Download token {endpoint.provisioning_token_configured ? '(configured)' : ''}</span>
              <input
                type="password"
                value={provisioningToken}
                autoComplete="new-password"
                placeholder="Leave blank to keep current token"
                disabled={busy}
                onChange={(event) => setProvisioningToken(event.target.value)}
              />
            </label>
            <button className="btn" type="submit" disabled={busy || (!inferenceToken && !provisioningToken)}>
              Save credentials
            </button>
            {endpoint.token_configured && (
              <button className="btn" type="button" disabled={busy} onClick={() => void updateLocalEndpoint(endpointId, { inference_token: null }).then(onReload).catch((caught) => setError(errMsg(caught)))}>
                Clear inference token
              </button>
            )}
            {endpoint.provisioning_token_configured && (
              <button className="btn" type="button" disabled={busy} onClick={() => void updateLocalEndpoint(endpointId, { provisioning_token: null }).then(onReload).catch((caught) => setError(errMsg(caught)))}>
                Clear download token
              </button>
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
      {error && <p className="settings-validation-message is-error">{error}</p>}
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
      <p className="settings-help" data-testid={`local-endpoint-probe-summary-${endpoint.endpoint_id}`}>
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

// Active/recent in-app pulls.
// Fetches its own list independently of the provider catalog -- pulls are a
// different resource with their own lifecycle -- and re-fetches whenever a
// live ModelPullProgress reports completion, so a finished download moves
// from "active" to "recent" without a manual reload.
function ModelPullList() {
  const [pulls, setPulls] = useState<ModelPullDto[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const result = await listModelPulls();
      setPulls(result.pulls);
      setError(null);
    } catch (caught) {
      setError(errMsg(caught));
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    // Defer the load so the setState fires outside the synchronous effect
    // body (matches LensList/WatchesPanel; avoids
    // react-hooks/set-state-in-effect cascades).
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const activePulls = pulls.filter((p) => p.status === 'pending' || p.status === 'running');
  const finishedPulls = pulls
    .filter((p) => p.status !== 'pending' && p.status !== 'running')
    .slice(0, 5);

  return (
    <div className="settings-subsection model-pull-list" data-testid="model-pull-list">
      {error && (
        <p
          className="settings-inline-status settings-validation-message is-error"
          data-testid="model-pull-list-error"
        >
          {error}
        </p>
      )}
      {activePulls.map((pull) => (
        <ModelPullProgress key={pull.id} pull={pull} onDone={() => void load()} />
      ))}
      {finishedPulls.length > 0 && (
        <ul className="model-pull-history">
          {finishedPulls.map((pull) => (
            <li
              key={pull.id}
              className="model-pull-history-row"
              data-testid={`model-pull-history-${pull.id}`}
            >
              <span className="model-pull-history-model">{pull.model}</span>
              <span className="model-pull-history-status">{pull.status}</span>
              <span className="model-pull-history-size">
                {pull.resolved_size != null ? formatNumberDisplay(pull.resolved_size, 'filesize') : '—'}
              </span>
            </li>
          ))}
        </ul>
      )}
      {loaded && !error && activePulls.length === 0 && finishedPulls.length === 0 && (
        <p className="settings-help">No downloads yet.</p>
      )}
    </div>
  );
}

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
  const [name, setName] = useState(prefill);
  const [value, setValue] = useState('');
  // Visible while typing (seeing what you paste beats blind entry —
  // the stored table only ever shows the last-4 hint anyway); the toggle
  // flips to password-masking for shoulder-surfing situations.
  const [valueVisible, setValueVisible] = useState(true);
  const valueRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (prefill) valueRef.current?.focus();
  }, [prefill]);
  const [notice, setNotice] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
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
    setMutationError(null);
    try {
      secrets.setData(await api.setProjectSecret(name, value));
      setName('');
      setValue('');
      setNotice('Secret saved');
    } catch (caught) {
      setNotice(null);
      setMutationError(errMsg(caught));
    }
  };
  return (
    <div className="settings-stack" data-testid="project-secrets-settings">
      <InlineStatus error={secrets.error ?? runtime.error ?? mutationError} loading={secrets.loading || runtime.loading} notice={notice} />
      {!canMutate && (
        <ReadOnlyNotice>
          Your project role can view secret status but cannot create, replace, or delete secrets.
        </ReadOnlyNotice>
      )}
      <form className="settings-form settings-inline-form" autoComplete="off" onSubmit={save}>
        <input
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
        <button
          className="btn"
          type="button"
          data-testid="secret-value-visibility"
          aria-label={valueVisible ? 'Hide secret value' : 'Show secret value'}
          aria-pressed={!valueVisible}
          onClick={() => setValueVisible((current) => !current)}
        >
          {valueVisible ? <EyeOff size={14} /> : <Eye size={14} />}
        </button>
        <button className="btn btn-primary" type="submit" disabled={!canMutate || !name.trim() || !value}><Save size={14} /> Save</button>
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
      <div className="settings-stack" data-testid="plugin-options-list">
        <InlineStatus error={plugins.length ? error : null} />
        <div className="settings-empty-state" data-testid="plugin-options-empty">
          No configurable plugin options.
        </div>
      </div>
    );
  }
  return (
    <div className="settings-stack" data-testid="plugin-options-list">
      <InlineStatus error={plugins.length ? error : null} />
      {rows.map(({ payload, setting }) => {
        const draft = drafts[payload.pluginId]?.[setting.id] ?? setting.effectiveValue ?? '';
        return (
          <form
            key={`${payload.pluginId}:${setting.id}`}
            className="settings-inline-form"
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
              <input
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
            <button
              className="btn"
              data-testid={`plugin-option-save-${setting.id}`}
              type="submit"
              disabled={!payload.canMutate || setting.readOnly}
            >
              Save
            </button>
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
      <div className="settings-stack" data-testid="project-plugins-settings">
        <ReadOnlyNotice>
          Plugin management is not available on this server.
        </ReadOnlyNotice>
      </div>
    );
  }
  return (
    <div className="settings-stack" data-testid="project-plugins-settings">
      <InlineStatus error={runtime.error} loading={runtime.loading} />
      {!canMutate && (
        <ReadOnlyNotice>
          Your project role can view plugin status but cannot change plugin lifecycle or settings.
        </ReadOnlyNotice>
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

function ProjectDataManagementSettings({ project }: { project?: ProjectInfo | null }) {
  const { projectApi: api, chromePreferences: { projectId } } = useWorkspaceStores();
  const retention = useLoader<ProjectRetentionPolicy>(() => api.getProjectRetention());
  const settings = useLoader<ProjectSettings>(() => api.getProjectSettings());
  const network = useLoader<ProjectNetworkPolicy>(() => api.getProjectNetworkPolicy());
  const [notice, setNotice] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [compactConfirm, setCompactConfirm] = useState('');
  const [compacting, setCompacting] = useState(false);
  const canMutate = canOwnProject(project);
  const canExport = canReviewProject(project);
  const policy = retention.data;
  const projectSettings = settings.data;
  const evidenceOptions = supportedEvidenceOptions(policy);
  const evidenceSelectOptions: PanelSelectOption[] = evidenceOptions.map((value) => ({
    value,
    label: EVIDENCE_RETENTION_LABELS[value] ?? value,
    description: EVIDENCE_RETENTION_DESCRIPTIONS[value]
      ?? 'Use the retention behavior provided by this project.',
  }));
  const patch = async (input: Partial<ProjectRetentionPolicy>) => {
    if (!canMutate) return;
    setMutationError(null);
    try {
      retention.setData(await api.updateProjectRetention(input));
      setNotice('Retention policy saved');
    } catch (caught) {
      setNotice(null);
      setMutationError(errMsg(caught));
    }
  };
  const patchSettings = async (input: Partial<ProjectSettings>) => {
    if (!canMutate) return;
    setMutationError(null);
    try {
      settings.setData(await api.updateProjectSettings(input));
      setNotice('Project settings saved');
    } catch (caught) {
      setNotice(null);
      setMutationError(errMsg(caught));
    }
  };
  const patchNetwork = async (mode: string) => {
    if (!canMutate) return;
    setMutationError(null);
    try {
      network.setData(await api.updateProjectNetworkPolicy({ mode }));
      setNotice('Network setting saved');
    } catch (caught) {
      setNotice(null);
      setMutationError(errMsg(caught));
    }
  };
  const compact = async (event: FormEvent) => {
    event.preventDefault();
    if (!canMutate || policy?.no_compact || compactConfirm !== 'COMPACT') return;
    setCompacting(true);
    setMutationError(null);
    try {
      const summary = await api.compactProject();
      setCompactConfirm('');
      if (summary.skipped) {
        setNotice('Compaction skipped because compaction is disabled for this project.');
      } else {
        const reclaimed = formatBytes(summary.db_bytes_reclaimed ?? summary.bytes_freed);
        const blobsRemoved = typeof summary.blobs_removed === 'number' ? summary.blobs_removed : 0;
        setNotice(`Compaction reclaimed ${reclaimed} and removed ${blobsRemoved} ${plural(blobsRemoved, 'blob')}.`);
      }
    } catch (caught) {
      setNotice(null);
      setMutationError(errMsg(caught));
    } finally {
      setCompacting(false);
    }
  };
  return (
    <div className="settings-stack" data-testid="project-data-management-settings">
      <InlineStatus
        error={retention.error ?? settings.error ?? network.error ?? mutationError}
        loading={retention.loading || settings.loading || network.loading}
        notice={notice}
      />
      {!canMutate && (
        <ReadOnlyNotice>
          Your project role can view retention settings but cannot change retention or compact the project.
        </ReadOnlyNotice>
      )}
      <section className="settings-subsection" data-testid="project-export-section">
        <div className="settings-section-subhead">
          <h2>Exports</h2>
          <p>Download portable copies of this project. Exports do not change project data.</p>
        </div>
        {canExport ? (
          <div className="settings-action-list">
            <div className="settings-action-row">
              <div>
                <strong>Export with media</strong>
                <p>Includes project data and stored media files.</p>
              </div>
              <a className="btn" data-testid="export-project-with-media" href={api.projectExportUrl(true)} download onClick={() => sendProductTelemetry({ type: 'Export.requested', properties: { exportKind: 'project_bundle' } }, projectId)}>
                <Download size={14} /> Download
              </a>
            </div>
            <div className="settings-action-row">
              <div>
                <strong>Export without media</strong>
                <p>Includes project structure and records, excluding blob media payloads.</p>
              </div>
              <a className="btn" data-testid="export-project-without-media" href={api.projectExportUrl(false)} download onClick={() => sendProductTelemetry({ type: 'Export.requested', properties: { exportKind: 'project_bundle_without_media' } }, projectId)}>
                <Archive size={14} /> Download
              </a>
            </div>
            <div className="settings-action-row">
              <div>
                <strong>Database snapshot</strong>
                <p>SQLite database snapshot for backup or inspection.</p>
              </div>
              <a className="btn" data-testid="export-project-database" href={api.projectExportUrl({ mode: 'db' })} download onClick={() => sendProductTelemetry({ type: 'Export.requested', properties: { exportKind: 'project_database' } }, projectId)}>
                <Database size={14} /> Download
              </a>
            </div>
          </div>
        ) : (
          <ReadOnlyNotice>
            A viewer can read this project but cannot take a full copy of it. Ask
            someone with reviewer access or higher to export it.
          </ReadOnlyNotice>
        )}
      </section>
      {policy && (
        <section className="settings-subsection">
          <div className="settings-section-subhead">
            <h2>Retention and evidence</h2>
            <p>
              Choose how much supporting evidence Frisket keeps for future review.
              This affects manual compaction, not the values already visible in your sheets.
            </p>
          </div>
          <div className="settings-grid-two">
          <label className="settings-field">
            <span>Evidence default</span>
            <PanelSelect
              testId="project-evidence-default"
              ariaLabel="Evidence default"
              value={policy.default_evidence}
              disabled={!canMutate}
              options={evidenceSelectOptions}
              onValueChange={(value) => void patch({ default_evidence: value })}
            />
            <small className="settings-field-help">
              This is the usual default. Frisket may keep particular evidence when an action needs it
              to preserve a result or its lineage.
            </small>
          </label>
          <label className="settings-check">
            <input
              type="checkbox"
              checked={policy.no_compact}
              disabled={!canMutate}
              onChange={(event) => void patch({ no_compact: event.currentTarget.checked })}
            />
            <span>Disable manual compaction</span>
          </label>
          </div>
        </section>
      )}
      {network.data && (
        <section className="settings-subsection" data-testid="project-network-section">
          <div className="settings-section-subhead">
            <h2>Network</h2>
            <p>
              Control whether project actions may contact outside services such as
              cloud AI providers, web search, geocoding, and public URLs. Turn this
              off for offline or especially sensitive work. Local tools and your
              configured local model servers still work.
            </p>
          </div>
          <label className="settings-field">
            <span>Network access</span>
            <PanelSelect
              data-testid="project-network-mode"
              value={network.data.mode}
              disabled={!canMutate}
              onChange={(event) => void patchNetwork(event.currentTarget.value)}
            >
              <option value="inherit">
                {`Inherit (currently ${network.data.org_default ?? 'on'})`}
              </option>
              <option value="on">On</option>
              <option value="off">Off</option>
            </PanelSelect>
            <small className="settings-field-help">
              {`Currently ${network.data.effective}. “Inherit” follows your organization’s
              setting. Plugins, notifications, and scheduled feed checks have their own
              network behavior.`}
            </small>
          </label>
        </section>
      )}
      {projectSettings && (
        <section className="settings-subsection" data-testid="project-media-downloads-section">
          <div className="settings-section-subhead">
            <h2>Media downloads</h2>
            <p>
              Decide whether media URLs may point to servers on the same private network
              as Frisket. Keep private addresses off when URLs come from imported,
              scraped, or otherwise untrusted data.
            </p>
          </div>
          {projectSettings.media_allow_private_hosts_locked ? (
            <small
              className="settings-field-help"
              data-testid="project-media-private-hosts-locked"
            >
              Downloads from private and loopback addresses are switched off and locked
              by the server (FRISKET_MEDIA_PRIVATE_HOSTS=deny-locked). There is no
              per-project override.
            </small>
          ) : (
            <>
              <label className="settings-check">
                <input
                  type="checkbox"
                  data-testid="project-media-allow-private-hosts"
                  checked={projectSettings.media_allow_private_hosts}
                  disabled={!canMutate}
                  onChange={(event) => void patchSettings({ media_allow_private_hosts: event.currentTarget.checked })}
                />
                <span>Allow downloads from private and loopback addresses</span>
              </label>
              <small className="settings-field-help">
                Turn this on only when you intentionally download from a server on your
                own network. Link-local and cloud metadata addresses remain blocked.
                Your administrator&apos;s network controls are always the final boundary.
              </small>
            </>
          )}
        </section>
      )}
      <form className="settings-danger-zone" data-testid="project-compact-danger-zone" onSubmit={compact}>
        <div className="settings-danger-header">
          <AlertTriangle size={16} />
          <div>
            <h2>Manual compaction</h2>
            <p>
              Free disk space by permanently deleting old run data and unused files
              that are no longer needed by the current project. Export a backup first
              if you may need that history later.
            </p>
          </div>
        </div>
        {policy?.no_compact && (
          <ReadOnlyNotice>
            Manual compaction is disabled by this project's retention policy.
          </ReadOnlyNotice>
        )}
        <label>
          <span>Type COMPACT to compact this project</span>
          <ConfirmTypeInput
            data-testid="compact-confirm-input"
            value={compactConfirm}
            disabled={!canMutate || compacting || policy?.no_compact}
            onChange={setCompactConfirm}
          />
        </label>
        <button
          className="btn btn-reject settings-danger-button"
          data-testid="compact-project-button"
          type="submit"
          disabled={!canMutate || compacting || policy?.no_compact || compactConfirm !== 'COMPACT'}
        >
          <Archive size={14} /> Compact
        </button>
      </form>
    </div>
  );
}

function SettingsTable({
  headers,
  rows,
  empty,
}: {
  headers: string[];
  rows: Array<Array<ReactNode>>;
  empty: string;
}) {
  if (rows.length === 0) return <div className="settings-empty-state">{empty}</div>;
  return (
    <div className="settings-table-wrap">
      <table className="settings-table">
        <thead>
          <tr>{headers.map((header) => <th key={header}>{header}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, idx, all) => {
            const content = row.map(String).join('|');
            // Content-based key; duplicate rows (possible in spend tables)
            // get an occurrence suffix instead of a bare array index.
            const occurrence = all
              .slice(0, idx)
              .filter((other) => other.map(String).join('|') === content).length;
            return (
            <tr key={occurrence ? `${content}#${occurrence}` : content}>
              {row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}
            </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function SettingsSectionRenderer({
  definition,
  project,
  identityMode,
}: {
  definition: OpenSettingsSectionDefinition;
  route: SettingsRoute;
  project?: ProjectInfo | null;
  identityMode: boolean;
}) {
  const body = (() => {
    switch (definition.component) {
      case 'personal.profile':
        return <PersonalProfileSettings identityMode={identityMode} />;
      case 'personal.preferences':
        return <PersonalPreferencesSettings />;
      case 'personal.privacy':
        return <PersonalPrivacySettings />;
      case 'personal.aiProviders':
        return <WorkspaceAiProvidersSettings />;
      case 'personal.diagnostics':
        return <DiagnosticsSettings />;
      case 'organization.aiProviders':
        return <OrganizationAiProvidersSettings />;
      case 'organization.secrets':
        return <OrganizationSecretsSettings />;
      case 'organization.apiKeys':
        return <OrganizationApiKeysSettings />;
      case 'organization.spend':
        return <OrganizationSpendSettings />;
      case 'project.general':
        return <ProjectGeneralSettings project={project} />;
      case 'project.access':
        return <ProjectAccessSettings project={project} identityMode={identityMode} />;
      case 'project.aiProviders':
        return <ProjectAiProvidersSettings project={project} />;
      case 'project.secrets':
        return <ProjectSecretsSettings project={project} />;
      case 'project.mcpServers':
        return <ProjectMcpServersSettings />;
      case 'project.notifications':
        return <NotificationSettingsPanel />;
      case 'project.plugins':
        return <ProjectPluginsSettings project={project} identityMode={identityMode} />;
      case 'project.dataManagement':
        return <ProjectDataManagementSettings project={project} />;
      default: {
        const unsupported: never = definition.component;
        throw new Error(`Unsupported open settings component: ${String(unsupported)}`);
      }
    }
  })();
  return <SectionFrame definition={definition}>{body}</SectionFrame>;
}
