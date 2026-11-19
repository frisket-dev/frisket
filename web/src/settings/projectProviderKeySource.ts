import type { ProjectProviderKeyInfo } from '../api/types';
import type { ProjectCloudRoutingView } from '../cloud/ProjectCloudRoutingSettings';

type ProjectProviderKeySource =
  | { kind: 'local'; label: string }
  | { kind: 'cloud'; label: string; settingsUrl: string }
  | { kind: 'missing'; label: 'Add key' };

/** Local project credentials take precedence over Cloud-held key metadata. */
export function projectProviderKeySource(
  provider: ProjectProviderKeyInfo,
  routing: ProjectCloudRoutingView | null,
): ProjectProviderKeySource {
  if (provider.configured) {
    return {
      kind: 'local',
      label: provider.hint ? `${provider.hint} · on this device` : 'On this device',
    };
  }
  const cloudKey = routing?.cloudProviderKeys.find((key) => (
    key.provider === provider.id && key.configured
  ));
  if (cloudKey) {
    const account = routing?.accounts.find((candidate) => (
      candidate.id === routing.selectedAccountId
    ));
    return {
      kind: 'cloud',
      label: `${account?.name ?? 'Cloud account'}’s key · kept in Frisket Cloud`,
      settingsUrl: cloudKey.settingsUrl,
    };
  }
  return { kind: 'missing', label: 'Add key' };
}
