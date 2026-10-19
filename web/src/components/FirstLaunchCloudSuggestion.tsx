import { Button } from './Button';
import { useEffect, useState } from 'react';
import { Modal } from './Modal';
import { cloudConnectionApi } from '../api/cloudConnection';
import { listProviders } from '../api/open';
import { CloudConnectionPanel } from '../cloud/CloudConnectionPanel';
import { notifyCloudConnectionChanged } from '../cloud/connectionChanged';

const DISMISSED_KEY = 'frisket.cloud-welcome.dismissed.v1';

/** Optional local onboarding; authentication remains owned by the shared panel. */
export function FirstLaunchCloudSuggestion({ projectId, onComplete }: {
  projectId?: string;
  onComplete(): void;
}) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    try {
      if (localStorage.getItem(DISMISSED_KEY)) { onComplete(); return; }
    } catch { /* Storage may be unavailable; dismissal still works this session. */ }
    const controller = new AbortController();
    // Optional guidance must not hold up other onboarding on a slow/offline server.
    const timeout = window.setTimeout(() => { controller.abort(); onComplete(); }, 5000);
    void Promise.all([
      cloudConnectionApi.getConnection(controller.signal),
      listProviders(projectId, { signal: controller.signal }),
    ]).then(([connection, catalog]) => {
      if (controller.signal.aborted) return;
      const configured = catalog.providers.some((provider) => provider.kind === 'platform_api'
        ? provider.configured : provider.reachable && provider.models.length > 0);
      if (connection.state !== 'disconnected' || !connection.capabilities.canConnect
          || catalog.network === 'off' || configured) {
        try { localStorage.setItem(DISMISSED_KEY, '1'); } catch { /* Optional persistence. */ }
        onComplete();
      } else setVisible(true);
    }).catch(() => {
      if (!controller.signal.aborted) onComplete();
    }).finally(() => window.clearTimeout(timeout));
    return () => { window.clearTimeout(timeout); controller.abort(); };
  }, [projectId, onComplete]);


  function dismiss() {
    try { localStorage.setItem(DISMISSED_KEY, '1'); } catch { /* Optional persistence. */ }
    onComplete();
  }

  if (!visible) return null;
  return <Modal title="Try AI with Frisket Cloud" onClose={dismiss}>
    <p>Use AI in Ask and actions without setting up API keys. You get $5 of free credit
      each month, and can also use your own keys or local models.</p>
    <div className="cloud-welcome-actions">
      <CloudConnectionPanel client={cloudConnectionApi} variant="welcome" onChanged={() => {
        notifyCloudConnectionChanged();
        dismiss();
      }} />
      <Button type="button" onClick={dismiss}>Continue without signing in</Button>
    </div>
  </Modal>;
}
