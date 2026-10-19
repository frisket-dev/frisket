import './PersonalSettings.css';
import { Button } from '../components/Button';
import { Modal } from '../components/Modal';
import { FormField, TextInput } from '../components/FormControls';
import { type FormEvent, type ComponentType, useEffect, useState } from 'react';
import { AlertTriangle, Save } from 'lucide-react';
import {
  ApiError,
  getMe,
  getRuntimeConfig,
  updateRuntimeConfig,
  updateLocalCostPreapproval,
  signOut,
  updateProfile,
  type RuntimeConfig,
  type MeInfo,
} from '../api/open';
import { type PersonalProfileSlotProps } from '../editions/module';
import { PanelSelect } from '../components/PanelSelect';
import { ConfirmTypeInput, PanelNotice } from '../components/PanelPrimitives';
import {
  applyPersonalPreferences,
  PREFERENCES_KEY,
  readPersonalPreferences,
  type Preferences,
} from './preferences';
import { CloudProfileSettings } from '../cloud/CloudProfileSettings';
import { cloudConnectionApi } from '../api/cloudConnection';
import { setTelemetryPreference, telemetryPreference } from '../telemetry/productTelemetry';
import { useLoader, errMsg } from './settingsSupport';
import { InlineStatus } from './SettingsPrimitives';

export function PersonalProfileSettings({
  identityMode,
  PersonalProfileSlot,
}: {
  identityMode: boolean;
  PersonalProfileSlot?: ComponentType<PersonalProfileSlotProps>;
}) {
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
    <div className="content-stack" data-testid="personal-profile-settings">
      <InlineStatus error={error ?? runtime.error} loading={loading || localRuntimeLoading} notice={notice} />
      <div className="settings-profile-row">
        <div className="settings-avatar" data-testid="settings-avatar">{initials}</div>
        <div>
          <div className="settings-meta-label">Email</div>
          <strong>{me?.email ?? 'Local workspace'}</strong>
        </div>
      </div>
      <form className="content-stack" onSubmit={save}>
        <FormField label="Display name" >

          <TextInput
            data-testid="personal-display-name"
            value={displayNameValue}
            maxLength={200}
            onChange={(event) => setDisplayName(event.currentTarget.value)}
          />
        </FormField>
        <FormField label="Cost pre-approval amount (USD)" >

          <TextInput
            data-testid="personal-cost-preapproval"
            type="number"
            min="0"
            step="0.000001"
            inputMode="decimal"
            value={costPreapprovalValue}
            onChange={(event) => setCostPreapproval(event.currentTarget.value)}
          />
          <small className="form-hint">
            Frisket always shows an estimate. Runs at or below this amount can use
            their selected provider without another prompt; higher or unknown costs
            require one click.
          </small>
        </FormField>
        <div className="control-row">
          <Button className="btn-primary" type="submit" disabled={busy || localRuntimeLoading}>
            <Save size={14} /> Save
          </Button>
          {identityMode && <Button

            type="button"
            data-testid="personal-sign-out"
            onClick={() => {
              void signOut().then(() => window.location.reload());
            }}
          >
            Sign out
          </Button>}
        </div>
      </form>
      {!identityMode && <CloudProfileSettings connectionClient={cloudConnectionApi} />}
      {PersonalProfileSlot && <PersonalProfileSlot />}
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
    <section className="content-stack ai-call-mode-settings" data-testid="ai-call-mode-settings">
      <div className="settings-section-kicker">AI call mode</div>
      <InlineStatus error={error} loading={config === null && error === null} notice={notice} />
      <p className="form-hint">
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
        <form className="content-stack" onSubmit={submit}>
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
          <div className="control-row">
            <Button
              type="submit"
              className="btn-primary"
              data-testid="ai-call-mode-save"
              disabled={busy || selected === null || selected === current}
            >
              <Save size={14} /> {busy ? 'Saving…' : 'Save mode'}
            </Button>
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
        <Modal title={<><AlertTriangle size={15} className="modal-warn-icon" /> Enable live AI calls?</>}
          testId="ai-call-mode-confirmation" busy={busy}
          onClose={() => { setPendingLiveMode(null); setConfirmText(''); }}>
          <form
            className="form-stack"
            onSubmit={(event) => {
              event.preventDefault();
              if (confirmText.trim().toLowerCase() === 'confirm') {
                void save(pendingLiveMode, true);
              }
            }}
          >
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
              <Button
                type="button"

                data-testid="ai-call-mode-confirm-cancel"
                disabled={busy}
                onClick={() => {
                  setPendingLiveMode(null);
                  setConfirmText('');
                }}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                className="btn-primary"
                data-testid="ai-call-mode-confirm"
                disabled={busy || confirmText.trim().toLowerCase() !== 'confirm'}
              >
                Enable live calls
              </Button>
            </div>
          </form>
        </Modal>
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

export function PersonalPreferencesSettings() {
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
      className="content-stack"
      data-testid="personal-preferences-settings"
      data-preferences-key={PREFERENCES_KEY}
    >
      <InlineStatus notice={notice} />
      <div className="settings-grid-two">
        <FormField label="Theme" >

          <PanelSelect
            data-testid="personal-pref-theme"
            value={prefs.theme}
            onChange={(event) => update({ theme: event.currentTarget.value as Preferences['theme'] })}
          >
            <option value="system">System</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </PanelSelect>
        </FormField>
      </div>
      <AiCallModeSettings />
    </div>
  );
}

export function PersonalPrivacySettings() {
  const [enabled, setEnabled] = useState(telemetryPreference() === 'enabled');
  const { data: config, error, loading } = useLoader<RuntimeConfig>(() => getRuntimeConfig());

  const update = (next: boolean) => {
    setTelemetryPreference(next);
    setEnabled(next);
  };

  return (
    <div className="content-stack" data-testid="personal-privacy-settings">
      <InlineStatus error={error} loading={loading} />
      <section className="content-stack">
        <div className="settings-section-kicker">Product telemetry</div>
        <p className="form-hint">
          Share pseudonymous feature use, outcomes, and coarse size and timing ranges.
          Product telemetry does not include your data, filenames, names, prompts, results,
          URLs, or error messages. The random browser identifier resets monthly, and
          sensitive projects send no project telemetry.
        </p>
        {config?.product_telemetry_available ? (
          <label className="control-row">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(event) => update(event.currentTarget.checked)}
              data-testid="product-telemetry-enabled"
            />
            <span>Send product telemetry</span>
          </label>
        ) : config ? (
          <PanelNotice>Product telemetry is not configured for this installation.</PanelNotice>
        ) : null}
      </section>
    </div>
  );
}
