import { describe, expect, it } from 'vitest';

import type { ProjectProviderKeyInfo } from '../../src/api/types';
import type { ProjectCloudRoutingView } from '../../src/cloud/ProjectCloudRoutingSettings';
import { projectProviderKeySource } from '../../src/settings/projectProviderKeySource';

const provider = (configured: boolean): ProjectProviderKeyInfo => ({
  id: 'openai', label: 'OpenAI', secret_name: 'OPENAI_API_KEY', kind: 'api',
  configured, hint: configured ? '••••8f2a' : null, policy_fields: [],
  spend_cap_usd: null, spent_usd: 0, unmetered_calls: 0, updated_at: null,
});

const routing: ProjectCloudRoutingView = {
  connectionState: 'connected', selectedAccountId: 41, canMutate: true,
  accounts: [{ id: 41, name: 'Riverton News', kind: 'organization', eligible: true }],
  cloudProviderKeys: [{ provider: 'openai', label: 'OpenAI', configured: true,
    settingsUrl: 'https://app.frisket.dev/funding-accounts?account_id=41&provider=openai' }],
};

describe('projectProviderKeySource', () => {
  it('uses local, then Cloud-held, then add-key precedence', () => {
    expect(projectProviderKeySource(provider(true), routing)).toEqual({
      kind: 'local', label: '••••8f2a · on this computer',
    });
    expect(projectProviderKeySource(provider(false), routing)).toEqual({
      kind: 'cloud', label: 'Riverton News’s key · kept in Frisket Cloud',
      settingsUrl: 'https://app.frisket.dev/funding-accounts?account_id=41&provider=openai',
    });
    expect(projectProviderKeySource(provider(false), { ...routing, cloudProviderKeys: [] })).toEqual({
      kind: 'missing', label: 'Add key',
    });
  });
});
