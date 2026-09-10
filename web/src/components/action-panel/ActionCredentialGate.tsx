import { AlertTriangle } from 'lucide-react';
import type { ActionTemplate } from '../../api/types';
import { navigate, useRoute } from '../../routes';

/** The catalog owns credential availability; both form hosts show its remedy. */
export function ActionCredentialGate({ actionTemplate }: { actionTemplate: ActionTemplate }) {
  const route = useRoute();
  const missing = actionTemplate.missingCredentials ?? [];
  if (!missing.length) return null;
  const openSettings = () => {
    navigate({ kind: 'settings', projectId: route.kind === 'project' ? route.projectId : undefined,
      scope: 'project', section: 'secrets' });
    window.history.replaceState(window.history.state, '',
      `${window.location.pathname}?secret=${encodeURIComponent(missing[0])}`);
  };
  return <div className="action-credential-gate" data-testid="action-credential-gate" role="alert">
    <AlertTriangle size={14} aria-hidden />
    <div className="action-credential-gate-copy">
      <strong>Needs an API key.</strong>
      <span> {actionTemplate.actionTitle ?? actionTemplate.name} requires {missing.join(', ')} to run.</span>
    </div>
    <button type="button" className="btn btn-secondary"
      data-testid="action-credential-gate-settings-link" onClick={openSettings}>
      Add in Settings →
    </button>
  </div>;
}
