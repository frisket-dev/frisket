import { ExternalLink } from 'lucide-react';

import type { EngineSelectorRoute } from './types';

export function EngineRouteDetails({ route }: { route: EngineSelectorRoute }) {
  const configuredKeyUrl = route.kind === 'local_key' ? route.settingsUrl : undefined;
  return (
    <dl className="engine-selector__route" data-testid="engine-selector-route">
      {(route.billedTo || configuredKeyUrl) && (
        <div>
          <dt>Billed to</dt>
          <dd>
            {configuredKeyUrl
              ? <a href={configuredKeyUrl}>{route.label} →</a>
              : <span>{route.billedTo}</span>}
            {route.balanceLabel && route.balanceUrl && (
              <a href={route.balanceUrl} target="_blank" rel="noreferrer">
                {route.balanceLabel}<ExternalLink size={12} aria-hidden />
              </a>
            )}
            {!configuredKeyUrl && route.hasAlternativeRoute && route.settingsUrl && (
              <a href={route.settingsUrl}>Change →</a>
            )}
          </dd>
        </div>
      )}
      {!configuredKeyUrl && route.credentialLabel && (
        <div>
          <dt>Key</dt>
          <dd>{route.credentialLabel}</dd>
        </div>
      )}
      {route.sendsDataTo && (
        <div>
          <dt>Sends data to</dt>
          <dd>{route.sendsDataTo}</dd>
        </div>
      )}
    </dl>
  );
}
