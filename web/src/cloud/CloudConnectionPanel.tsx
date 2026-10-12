import { CheckCircle2, Copy, ExternalLink, LogOut } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';

export type CloudConnectionState = 'disconnected' | 'connected' | 'expired';

export interface CloudAccountView {
  id: number;
  name: string;
  kind: 'personal' | 'organization';
  eligible: boolean;
  eligibilityReason?: string;
  balanceLabel?: string;
  balanceUrl?: string;
}

const cloudAccountKindLabel = (kind: CloudAccountView['kind']): string => (
  kind === 'personal' ? 'Personal' : 'Organization'
);

export interface CloudConnectionView {
  state: CloudConnectionState;
  identity: { id: number; displayName: string; email: string } | null;
  expiresAt?: string;
  accounts: CloudAccountView[];
  capabilities: { canConnect: boolean; canReconnect: boolean; canDisconnect: boolean };
  notice?: string;
}

export interface CloudDeviceAuthorizationView {
  id: string;
  userCode: string;
  verificationUri: string;
  verificationUriComplete?: string;
  expiresAt: string;
  pollIntervalSeconds: number;
}

export interface CloudConnectionClient {
  getConnection(signal?: AbortSignal): Promise<CloudConnectionView>;
  startAuthorization(signal?: AbortSignal): Promise<CloudDeviceAuthorizationView>;
  pollAuthorization(id: string, signal?: AbortSignal): Promise<{
    status: 'pending' | 'declined' | 'expired' | 'connected';
    connection: CloudConnectionView;
  }>;
  cancelAuthorization(id: string, signal?: AbortSignal): Promise<void>;
  disconnect(signal?: AbortSignal): Promise<CloudConnectionView>;
}

interface CloudConnectionPanelProps {
  client: CloudConnectionClient;
  variant: 'profile' | 'compact';
  allowSignOut?: boolean;
  promptForAccount?: boolean;
  intent?: 'connect' | 'reconnect';
  onSelectAccount?(accountId: number): Promise<void>;
  onChanged?(): void;
}

type Phase = 'idle' | 'starting' | 'waiting' | 'account' | 'connected';

const connectionError = (caught: unknown): string => (
  caught instanceof Error && caught.message.trim()
    ? caught.message
    : 'Could not connect to Frisket Cloud. Try again.'
);

export function CloudConnectionPanel({
  client,
  variant,
  allowSignOut = false,
  promptForAccount = false,
  intent,
  onSelectAccount,
  onChanged,
}: CloudConnectionPanelProps) {
  const [connection, setConnection] = useState<CloudConnectionView | null>(null);
  const [authorization, setAuthorization] = useState<CloudDeviceAuthorizationView | null>(null);
  const [phase, setPhase] = useState<Phase>('idle');
  const [accountId, setAccountId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  const timer = useRef<number | null>(null);
  const selectAccountRef = useRef(onSelectAccount);
  const onChangedRef = useRef(onChanged);

  useEffect(() => {
    selectAccountRef.current = onSelectAccount;
    onChangedRef.current = onChanged;
  }, [onChanged, onSelectAccount]);

  const clearPending = useCallback(() => {
    request.current?.abort();
    request.current = null;
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
  }, []);

  const finishConnection = useCallback(async (fresh: CloudConnectionView) => {
    setConnection(fresh);
    const eligible = fresh.accounts.filter((account) => account.eligible);
    const selectAccount = selectAccountRef.current;
    if (!selectAccount) {
      setPhase('connected');
      onChangedRef.current?.();
      return;
    }
    if (eligible.length === 1) {
      setBusy(true);
      try {
        await selectAccount(eligible[0].id);
        setPhase('connected');
        onChangedRef.current?.();
      } catch (caught) {
        setError(connectionError(caught));
        setPhase('account');
      } finally {
        setBusy(false);
      }
      return;
    }
    setAccountId(eligible[0]?.id ?? null);
    setPhase('account');
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    request.current = controller;
    void client.getConnection(controller.signal).then((fresh) => {
      setConnection(fresh);
      if (promptForAccount && fresh.state === 'connected') void finishConnection(fresh);
    }, (caught: unknown) => {
      if (!controller.signal.aborted) setError(connectionError(caught));
    });
    return clearPending;
  }, [clearPending, client, finishConnection, promptForAccount]);

  const poll = (started: CloudDeviceAuthorizationView) => {
    timer.current = window.setTimeout(() => {
      const controller = new AbortController();
      request.current = controller;
      void client.pollAuthorization(started.id, controller.signal).then((result) => {
        if (result.status === 'pending') {
          poll(started);
          return;
        }
        request.current = null;
        if (result.status === 'connected') {
          setAuthorization(null);
          void finishConnection(result.connection);
          return;
        }
        setConnection(result.connection);
        setAuthorization(null);
        setPhase('idle');
        setError('Sign-in didn’t finish. It was declined in the browser or the code expired. Try again.');
      }, (caught: unknown) => {
        if (!controller.signal.aborted) {
          setPhase('idle');
          setError(connectionError(caught));
        }
      });
    }, Math.max(1, started.pollIntervalSeconds) * 1000);
  };

  const begin = async () => {
    clearPending();
    setBusy(true);
    setError(null);
    setPhase('starting');
    const controller = new AbortController();
    request.current = controller;
    try {
      const started = await client.startAuthorization(controller.signal);
      setAuthorization(started);
      setPhase('waiting');
      poll(started);
    } catch (caught) {
      if (!controller.signal.aborted) {
        setPhase('idle');
        setError(connectionError(caught));
      }
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    const active = authorization;
    clearPending();
    setAuthorization(null);
    setPhase('idle');
    setError(null);
    if (active) {
      const controller = new AbortController();
      try { await client.cancelAuthorization(active.id, controller.signal); } catch { /* best effort */ }
    }
  };

  const continueWithAccount = async () => {
    if (accountId === null || !onSelectAccount) return;
    setBusy(true);
    setError(null);
    try {
      await onSelectAccount(accountId);
      setPhase('connected');
      onChanged?.();
    } catch (caught) {
      setError(connectionError(caught));
    } finally {
      setBusy(false);
    }
  };

  const signOut = async () => {
    setBusy(true);
    setError(null);
    const controller = new AbortController();
    request.current = controller;
    try {
      setConnection(await client.disconnect(controller.signal));
      setPhase('idle');
      onChanged?.();
    } catch (caught) {
      if (!controller.signal.aborted) setError(connectionError(caught));
    } finally {
      setBusy(false);
    }
  };

  if (!connection) {
    return <section className={`cloud-connection-card is-${variant}`}>
      <p className="settings-help">Checking Frisket Cloud connection…</p>
      {error && <p className="cloud-connection-error" role="alert">{error}</p>}
    </section>;
  }

  if (phase === 'waiting' && authorization) {
    return <section className={`cloud-connection-card is-${variant}`} data-testid="cloud-connection-waiting">
      <h3>Finish signing in in your browser</h3>
      <p>Check that this code matches the one shown by Frisket Cloud.</p>
      <strong className="cloud-connection-code">{authorization.userCode}</strong>
      <div className="cloud-connection-actions">
        <a className="btn btn-primary"
          href={authorization.verificationUriComplete ?? authorization.verificationUri}
          target="_blank" rel="noreferrer">
          Open sign-in page <ExternalLink size={14} aria-hidden />
        </a>
        <button className="btn" type="button" onClick={() => void cancel()}>Cancel</button>
        <button className="btn" type="button" onClick={() => {
          void navigator.clipboard.writeText(
            authorization.verificationUriComplete ?? authorization.verificationUri,
          );
        }}><Copy size={14} /> Copy sign-in link</button>
      </div>
    </section>;
  }

  const eligibleAccounts = connection.accounts.filter((account) => account.eligible);
  if (phase === 'account') {
    return <section className={`cloud-connection-card is-${variant}`} data-testid="cloud-account-step">
      <h3>Which account should pay?</h3>
      <p>Choose the Frisket Cloud account this project will use.</p>
      <fieldset className="cloud-account-options">
        <legend className="sr-only">Cloud account</legend>
        {connection.accounts.map((account) => <label key={account.id}
          className={account.eligible ? undefined : 'is-disabled'}>
          <input type="radio" name="cloud-account" value={account.id}
            disabled={!account.eligible}
            checked={accountId === account.id} onChange={() => setAccountId(account.id)} />
          <span><strong>{account.name}</strong><small>{account.eligibilityReason
            ?? `${cloudAccountKindLabel(account.kind)}${account.balanceLabel ? ` · ${account.balanceLabel}` : ''}`}</small></span>
        </label>)}
      </fieldset>
      {eligibleAccounts.length === 0 && <p className="cloud-connection-error" role="alert">
        None of these accounts can fund this project.
      </p>}
      <button className="btn btn-primary" type="button" disabled={busy || accountId === null}
        onClick={() => void continueWithAccount()}>{busy ? 'Connecting…' : 'Continue'}</button>
      {error && <p className="cloud-connection-error" role="alert">{error}</p>}
    </section>;
  }

  if (connection.state === 'connected') {
    return <section className={`cloud-connection-card is-${variant} is-connected`} data-testid="cloud-connection-connected">
      <div className="cloud-connection-heading">
        <CheckCircle2 size={18} aria-hidden />
        <div><h3>Connected to Frisket Cloud</h3>
          <p>{connection.identity?.displayName || connection.identity?.email}</p></div>
      </div>
      {variant === 'profile' && connection.accounts.length > 0 && (
        <ul className="cloud-connection-accounts">
          {connection.accounts.map((account) => <li key={account.id}>
            <span><strong>{account.name}</strong> · {cloudAccountKindLabel(account.kind)}</span>
            {account.balanceLabel && account.balanceUrl && <a href={account.balanceUrl}
              target="_blank" rel="noreferrer">{account.balanceLabel}<ExternalLink size={12} aria-hidden /></a>}
          </li>)}
        </ul>
      )}
      {phase === 'connected' && variant === 'compact' && <p className="cloud-connection-success">This project can now use Frisket Cloud.</p>}
      {allowSignOut && connection.capabilities.canDisconnect && (
        <button className="btn" type="button" disabled={busy} onClick={() => void signOut()}>
          <LogOut size={14} /> {busy ? 'Signing out…' : 'Sign out'}
        </button>
      )}
      {error && <p className="cloud-connection-error" role="alert">{error}</p>}
    </section>;
  }

  const expired = intent === 'reconnect' || connection.state === 'expired';
  const permitted = connection.state === 'expired'
    ? connection.capabilities.canReconnect
    : connection.capabilities.canConnect;
  return <section className={`cloud-connection-card is-${variant}`} data-testid="cloud-connection-card">
    <h3>{expired ? 'Reconnect Frisket Cloud' : 'Connect Frisket Cloud'}</h3>
    <p>{expired
      ? 'This computer was signed out of Frisket Cloud. Your settings are unchanged.'
      : 'Sign in with your Frisket account. No API key is needed, and this project stays on this computer.'}</p>
    {!expired && <p>Each person gets $5 of free Frisket credit every month.</p>}
    <button className="btn btn-primary" type="button" disabled={busy || !permitted}
      onClick={() => void begin()}>{phase === 'starting' ? 'Opening browser…' : expired ? 'Reconnect' : <><span>Sign in with browser</span><ExternalLink size={14} /></>}</button>
    {expired && allowSignOut && connection.capabilities.canDisconnect && (
      <button className="btn" type="button" disabled={busy} onClick={() => void signOut()}>
        <LogOut size={14} /> {busy ? 'Signing out…' : 'Sign out'}
      </button>
    )}
    {connection.notice && <p className="settings-help" role="status">{connection.notice}</p>}
    {error && <p className="cloud-connection-error" role="alert">{error}</p>}
  </section>;
}
