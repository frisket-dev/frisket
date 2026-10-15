import { useEffect, useRef, useState } from 'react';
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
  const dialogRef = useRef<HTMLDialogElement>(null);

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

  useEffect(() => {
    if (visible) dialogRef.current?.showModal();
  }, [visible]);

  function dismiss() {
    try { localStorage.setItem(DISMISSED_KEY, '1'); } catch { /* Optional persistence. */ }
    onComplete();
  }

  if (!visible) return null;
  return <dialog ref={dialogRef} className="modal-card" aria-labelledby="cloud-welcome-title"
    onCancel={(event) => { event.preventDefault(); dismiss(); }}>
    <h2 className="modal-title" id="cloud-welcome-title">Try AI with Frisket Cloud</h2>
    <p>Use Cloud models in Ask and actions without setting up API keys. Model usage is paid
      from your Cloud credits; signing in is optional.</p>
    <CloudConnectionPanel client={cloudConnectionApi} variant="compact" onChanged={() => {
      notifyCloudConnectionChanged();
      dismiss();
    }} />
    <p>You can also use your own API keys or local models, and connect Cloud later in Settings.</p>
    <div className="form-actions">
      <button type="button" className="btn" onClick={dismiss}>Continue without signing in</button>
    </div>
  </dialog>;
}
