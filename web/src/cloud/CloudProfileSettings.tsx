import { CloudConnectionPanel, type CloudConnectionClient } from './CloudConnectionPanel';

export function CloudProfileSettings({
  connectionClient,
}: {
  connectionClient: CloudConnectionClient;
}) {
  return <section className="settings-stack" id="frisket-cloud" data-testid="cloud-profile-settings">
    <div className="settings-section-subhead">
      <h2>Frisket Cloud</h2>
      <p>Connect this Frisket installation to use Cloud accounts and credits in local projects.</p>
    </div>
    <CloudConnectionPanel client={connectionClient} variant="profile" allowSignOut />
  </section>;
}
