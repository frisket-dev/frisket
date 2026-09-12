import { useEffect, useState } from 'react';
import type { HttpModelsGatewayStatus } from '../generated/openHttpContracts';
import { getModelsGateway, type ModelsGatewayScope } from '../api/modelsGateway';
import { ModelsGatewayForm } from './ModelsGatewayForm';
import { useSetupRequests } from './setupLifetime';

/** Mounted only where the edition contributes the gateway status resource. */
function ScopedModelsGatewaySettings({ scope }: { scope: ModelsGatewayScope }) {
  const [status, setStatus] = useState<HttpModelsGatewayStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const { begin } = useSetupRequests();
  const load = () => {
    const request = begin();
    return getModelsGateway(scope, request.signal).then(
      (fresh) => { if (request.isCurrent()) { setStatus(fresh); setLoading(false); } },
      () => { if (request.isCurrent()) { setError('Could not load models gateway configuration.'); setLoading(false); } },
    );
  };
  useEffect(() => {
    const request = begin();
    getModelsGateway(scope, request.signal).then(
      (fresh) => { if (request.isCurrent()) { setStatus(fresh); setLoading(false); } },
      () => { if (request.isCurrent()) { setError('Could not load models gateway configuration.'); setLoading(false); } },
    );
  }, [begin, scope]);
  const reload = () => { setLoading(true); setError(null); void load(); };
  return <section className="settings-subsection" data-testid="models-gateway-settings">
    <div className="settings-section-subhead"><h2>Models gateway</h2>
      <p>Attach a Frisket models server with its URL and bearer token.</p>
    </div>
    {loading && <p role="status">Loading models gateway configuration…</p>}
    {error && <p role="alert">{error}</p>}
    {status && <>
      <p className="settings-help">{status.configured ? `Configured at ${status.origin ?? 'the configured server'}${status.token_hint ? ` (${status.token_hint})` : ''}` : 'No models gateway configured.'}</p>
      {status.error && <p role="alert">{status.error}</p>}
      {status.can_mutate ? <ModelsGatewayForm key={`${scope}:${status.source}:${status.origin}`} scope={scope} initialOrigin={status.origin ?? ''} onSaved={reload} />
        : <p className="settings-help">{status.source === 'environment'
          ? `Read-only configuration. Change ${status.environment_names.join(' and ')} in the server environment.`
          : 'Read-only configuration. Ask an organization owner to change the models gateway.'}</p>}
    </>}
    <button className="btn" type="button" disabled={loading} onClick={reload}>Recheck configuration</button>
  </section>;
}

export function ModelsGatewaySettings({ scope }: { scope: ModelsGatewayScope }) {
  return <ScopedModelsGatewaySettings key={scope} scope={scope} />;
}
