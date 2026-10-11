import { useEffect, useState } from 'react';

import { CloudConnectionPanel, type CloudConnectionClient } from './CloudConnectionPanel';
import type { CloudExecutionPreference } from './ProjectCloudRoutingSettings';

export interface CloudConnectionPreferencesClient {
  get(signal?: AbortSignal): Promise<CloudExecutionPreference>;
  patch(preference: CloudExecutionPreference, signal?: AbortSignal): Promise<CloudExecutionPreference>;
}

export function CloudProfileSettings({
  connectionClient,
  preferencesClient,
}: {
  connectionClient: CloudConnectionClient;
  preferencesClient: CloudConnectionPreferencesClient;
}) {
  const [preference, setPreference] = useState<CloudExecutionPreference | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void preferencesClient.get(controller.signal).then(setPreference, (caught: unknown) => {
      if (!controller.signal.aborted) setError(caught instanceof Error ? caught.message : String(caught));
    });
    return () => controller.abort();
  }, [preferencesClient]);

  const update = async (next: CloudExecutionPreference) => {
    setBusy(true);
    setError(null);
    const controller = new AbortController();
    try { setPreference(await preferencesClient.patch(next, controller.signal)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setBusy(false); }
  };

  return <section className="settings-stack" id="frisket-cloud" data-testid="cloud-profile-settings">
    <div className="settings-section-subhead">
      <h2>Frisket Cloud</h2>
      <p>Connect this Frisket installation to use Cloud accounts and credits in local projects.</p>
    </div>
    <CloudConnectionPanel client={connectionClient} variant="profile" allowSignOut />
    {preference && <fieldset className="cloud-preference-options" disabled={busy}>
      <legend>Default for new projects</legend>
      <p className="settings-help">Used when a model can run with both a local key and Cloud.</p>
      <label>
        <input type="radio" name="cloud-default-preference" checked={preference === 'local_first'}
          onChange={() => void update('local_first')} />
        <span><strong>Use your keys on this computer</strong>
          <small>Keeps the behavior from before Cloud was connected.</small></span>
      </label>
      <label>
        <input type="radio" name="cloud-default-preference" checked={preference === 'cloud_first'}
          onChange={() => void update('cloud_first')} />
        <span><strong>Use the project’s Cloud account</strong>
          <small>Uses its credits or provider keys where available.</small></span>
      </label>
    </fieldset>}
    {error && <div className="settings-inline-error" role="alert">{error}</div>}
  </section>;
}
