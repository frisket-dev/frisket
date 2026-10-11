import { useCallback, useEffect, useState } from 'react';

import {
  CloudConnectionPanel,
  type CloudAccountView,
  type CloudConnectionClient,
} from './CloudConnectionPanel';

const cloudAccountKindLabel = (kind: CloudAccountView['kind']): string => (
  kind === 'personal' ? 'Personal' : 'Organization'
);

export type CloudExecutionPreference = 'local_first' | 'cloud_first';

export interface ProjectCloudRoutingView {
  connectionState: 'disconnected' | 'connected' | 'expired';
  selectedAccountId: number | null;
  preference: CloudExecutionPreference;
  preferenceInherited: boolean;
  accounts: CloudAccountView[];
  overlapProviderLabels: string[];
  canMutate: boolean;
}

export interface ProjectCloudRoutingClient {
  get(projectId: string, signal?: AbortSignal): Promise<ProjectCloudRoutingView>;
  patch(projectId: string, patch: {
    selectedAccountId?: number;
    preference?: CloudExecutionPreference;
  }, signal?: AbortSignal): Promise<ProjectCloudRoutingView>;
}

interface ProjectCloudRoutingSettingsProps {
  projectId: string;
  connectionClient: CloudConnectionClient;
  routingClient: ProjectCloudRoutingClient;
}

export function ProjectCloudRoutingSettings({
  projectId,
  connectionClient,
  routingClient,
}: ProjectCloudRoutingSettingsProps) {
  const [routing, setRouting] = useState<ProjectCloudRoutingView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => {
    const controller = new AbortController();
    void routingClient.get(projectId, controller.signal).then(setRouting, (caught: unknown) => {
      if (!controller.signal.aborted) setError(caught instanceof Error ? caught.message : String(caught));
    });
    return controller;
  }, [projectId, routingClient]);

  useEffect(() => {
    const controller = load();
    return () => controller.abort();
  }, [load]);

  const patch = async (
    change: { selectedAccountId?: number; preference?: CloudExecutionPreference },
    propagate = false,
  ) => {
    setBusy(true);
    setError(null);
    const controller = new AbortController();
    try {
      setRouting(await routingClient.patch(projectId, change, controller.signal));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      if (propagate) throw caught;
    } finally {
      setBusy(false);
    }
  };

  if (!routing) {
    return <section className="settings-stack" data-testid="project-cloud-routing">
      <div className={error ? 'settings-inline-error' : 'settings-inline-status'}
        role={error ? 'alert' : undefined}>{error || 'Loading Frisket Cloud settings…'}</div>
    </section>;
  }

  const eligibleAccounts = routing.accounts.filter((account) => account.eligible);
  const selected = eligibleAccounts.find((account) => account.id === routing.selectedAccountId) ?? null;
  const overlap = routing.overlapProviderLabels.length
    ? routing.overlapProviderLabels.join(', ')
    : null;

  return <section className="settings-stack project-cloud-routing" data-testid="project-cloud-routing">
    <div className="settings-section-subhead">
      <h2>Frisket Cloud</h2>
      <p>Choose the Cloud account for this project and how overlapping models should run.</p>
    </div>
    {routing.connectionState !== 'connected' && (
      <CloudConnectionPanel client={connectionClient} variant="compact"
        onSelectAccount={(id) => patch({ selectedAccountId: id }, true).then(() => undefined)}
        onChanged={() => { load(); }} />
    )}
    {routing.connectionState === 'connected' && <label className="settings-form-field">
      <span>Cloud account for this project</span>
      <select value={selected?.id ?? ''} disabled={!routing.canMutate || busy}
        onChange={(event) => void patch({ selectedAccountId: Number(event.currentTarget.value) })}>
        <option value="" disabled>Choose an account</option>
        {eligibleAccounts.map((account) => <option key={account.id} value={account.id}>
          {account.name} · {cloudAccountKindLabel(account.kind)}
        </option>)}
      </select>
      {selected?.balanceLabel && <small className="settings-help">
        {selected.balanceUrl
          ? <a href={selected.balanceUrl} target="_blank" rel="noreferrer">Frisket credits · {selected.balanceLabel}</a>
          : <>Frisket credits · {selected.balanceLabel}</>}
      </small>}
    </label>}

    <fieldset className="cloud-preference-options" disabled={!routing.canMutate || busy || routing.connectionState !== 'connected'}>
      <legend>When a model works with both</legend>
      <p className="settings-help">{overlap
        ? `Both routes can run ${overlap}. Choose which one this project uses.`
        : 'No configured provider currently overlaps, so this setting has no effect.'}</p>
      <label>
        <input type="radio" name="cloud-preference" value="local_first"
          checked={routing.preference === 'local_first'}
          onChange={() => void patch({ preference: 'local_first' })} />
        <span><strong>Use your keys on this computer</strong>
          <small>How this project ran before connecting Cloud.</small></span>
      </label>
      <label>
        <input type="radio" name="cloud-preference" value="cloud_first"
          checked={routing.preference === 'cloud_first'}
          onChange={() => void patch({ preference: 'cloud_first' })} />
        <span><strong>Use {selected?.name ?? 'the project’s Cloud account'}</strong>
          <small>Frisket credits, or the account’s own keys where it has them.</small></span>
      </label>
    </fieldset>
    <p className="settings-help">{routing.preferenceInherited
      ? 'This project currently uses the installation default. '
      : 'This choice applies only to this project. '}
      <a href="/settings/personal/profile#frisket-cloud">Change installation default →</a>
    </p>
    {error && <div className="settings-inline-error" role="alert">{error}</div>}
  </section>;
}
