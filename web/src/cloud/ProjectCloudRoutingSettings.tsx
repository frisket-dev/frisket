import { FormField } from '../components/FormControls';
import { useCallback, useEffect, useState } from 'react';

import {
  CloudConnectionPanel,
  type CloudAccountView,
  type CloudConnectionClient,
} from './CloudConnectionPanel';

const cloudAccountKindLabel = (kind: CloudAccountView['kind']): string => (
  kind === 'personal' ? 'Personal' : 'Organization'
);

export interface ProjectCloudProviderKeyView {
  provider: string;
  label: string;
  configured: boolean;
  settingsUrl: string;
}

export interface ProjectCloudRoutingView {
  connectionState: 'disconnected' | 'connected' | 'expired';
  selectedAccountId: number | null;
  accounts: CloudAccountView[];
  cloudProviderKeys: ProjectCloudProviderKeyView[];
  canMutate: boolean;
}

export interface ProjectCloudRoutingClient {
  get(projectId: string, signal?: AbortSignal): Promise<ProjectCloudRoutingView>;
  patch(projectId: string, patch: {
    selectedAccountId?: number;
  }, signal?: AbortSignal): Promise<ProjectCloudRoutingView>;
}

interface ProjectCloudRoutingSettingsProps {
  projectId: string;
  connectionClient: CloudConnectionClient;
  routingClient: ProjectCloudRoutingClient;
  onRoutingChange?(routing: ProjectCloudRoutingView | null): void;
}

export function ProjectCloudRoutingSettings({
  projectId,
  connectionClient,
  routingClient,
  onRoutingChange,
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

  useEffect(() => {
    onRoutingChange?.(routing);
  }, [onRoutingChange, routing]);

  const patch = async (
    change: { selectedAccountId?: number },
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
    return <section className="content-stack" data-testid="project-cloud-routing">
      <div className={error ? 'panel-error' : 'panel-notice'}
        role={error ? 'alert' : undefined}>{error || 'Loading Frisket Cloud settings…'}</div>
    </section>;
  }

  const selected = routing.accounts.find((account) => account.id === routing.selectedAccountId) ?? null;
  const selectedAccountUnavailable = routing.selectedAccountId !== null && !selected?.eligible;
  return <section className="content-stack project-cloud-routing" data-testid="project-cloud-routing">
    <div className="settings-section-subhead">
      <h2>Frisket Cloud account</h2>
      <p>Pays for models in this project that run through Frisket Cloud.</p>
    </div>
    {routing.connectionState !== 'connected' && (
      <CloudConnectionPanel client={connectionClient} variant="compact"
        intent={routing.connectionState === 'expired' ? 'reconnect' : 'connect'}
        onSelectAccount={routing.connectionState === 'expired' ? undefined
          : (id) => patch({ selectedAccountId: id }, true).then(() => undefined)}
        onChanged={() => { load(); }} />
    )}
    {routing.connectionState === 'connected' && <FormField label="Cloud account for this project" >

      <select value={routing.selectedAccountId ?? ''} disabled={!routing.canMutate || busy}
        onChange={(event) => void patch({ selectedAccountId: Number(event.currentTarget.value) })}>
        <option value="" disabled>Choose an account</option>
        {routing.selectedAccountId !== null && !selected && <option value={routing.selectedAccountId} disabled>
          Previously selected account · unavailable
        </option>}
        {routing.accounts.map((account) => <option key={account.id} value={account.id}
          disabled={!account.eligible}>
          {account.name} · {cloudAccountKindLabel(account.kind)}{account.eligible ? '' : ' · unavailable'}
        </option>)}
      </select>
      {selected?.balanceLabel && <small className="form-hint">
        {selected.balanceUrl
          ? <a href={selected.balanceUrl} target="_blank" rel="noreferrer">Frisket credits · {selected.balanceLabel}</a>
          : <>Frisket credits · {selected.balanceLabel}</>}
      </small>}
    </FormField>}
    {routing.connectionState === 'connected' && selectedAccountUnavailable && (
      <div className="panel-error" role="alert">
        {selected?.name ?? 'The selected Cloud account'} can no longer fund this project.
        Choose another eligible account; Frisket won’t switch it automatically.
      </div>
    )}

    {error && <div className="panel-error" role="alert">{error}</div>}
  </section>;
}
