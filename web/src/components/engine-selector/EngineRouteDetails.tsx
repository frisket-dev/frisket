import { ExternalLink } from 'lucide-react';

import type { EngineSelectorRoute } from './types';

export function EngineRouteDetails({ route }: { route: EngineSelectorRoute }) {
  return (
    <dl className="engine-selector__route" data-testid="engine-selector-route">
      <div>
        <dt>Runs with</dt>
        <dd><strong>{route.label}</strong></dd>
      </div>
      {route.billedTo && (
        <div>
          <dt>Billed to</dt>
          <dd>
            <span>{route.billedTo}</span>
            {route.balanceLabel && route.balanceUrl && (
              <a href={route.balanceUrl} target="_blank" rel="noreferrer">
                {route.balanceLabel}<ExternalLink size={12} aria-hidden />
              </a>
            )}
            {route.hasAlternativeRoute && route.settingsUrl && (
              <a href={route.settingsUrl}>Change →</a>
            )}
          </dd>
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
