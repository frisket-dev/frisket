import '../settings/SettingsPrimitives.css';
import { Button } from '../components/Button';
import { getModelsGateway, type ModelsGatewayScope } from '../api/modelsGateway';
import { ModelsGatewayForm } from './ModelsGatewayForm';
import { useLoader } from '../hooks/useLoader';

/** Mounted only where the edition contributes the gateway status resource. */
function ScopedModelsGatewaySettings({ scope }: { scope: ModelsGatewayScope }) {
  const { data: status, error, loading, reload: refetch } = useLoader((signal) => getModelsGateway(scope, signal), [scope]);
  const reload = () => { void refetch(); };
  return <section className="content-stack" data-testid="models-gateway-settings">
    <div className="settings-section-subhead"><h2>Models gateway</h2>
      <p>Attach a Frisket models server with its URL and bearer token.</p>
    </div>
    {loading && <p role="status">Loading models gateway configuration…</p>}
    {error !== null && <p role="alert">Could not load models gateway configuration.</p>}
    {status && <>
      <p className="form-hint">{status.configured ? `Configured at ${status.origin ?? 'the configured server'}${status.token_hint ? ` (${status.token_hint})` : ''}` : 'No models gateway configured.'}</p>
      {status.error && <p role="alert">{status.error}</p>}
      {status.can_mutate ? <ModelsGatewayForm key={`${scope}:${status.source}:${status.origin}`} scope={scope} initialOrigin={status.origin ?? ''} onSaved={reload} />
        : <p className="form-hint">{status.source === 'environment'
          ? `Read-only configuration. Change ${status.environment_names.join(' and ')} in the server environment.`
          : 'Read-only configuration. Ask an organization owner to change the models gateway.'}</p>}
    </>}
    <Button type="button" disabled={loading} onClick={reload}>Recheck configuration</Button>
  </section>;
}

export function ModelsGatewaySettings({ scope }: { scope: ModelsGatewayScope }) {
  return <ScopedModelsGatewaySettings key={scope} scope={scope} />;
}
