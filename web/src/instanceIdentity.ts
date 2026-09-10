// Instance identity: lets a third-party operator's own display name/support
// contact replace our hardcoded branding on the pre-auth sign-in page and in
// app chrome (BrandLink), without special-casing our own hosted deployment — it
// is simply whichever org is configured first. Local tier has no `/api/instance`
// (404, same as `/api/me`) and mock mode never calls it, so both fall back to
// the generic default below.
import { useEffect, useState } from 'react';
import { createInstanceRuntimeApi } from './api/instanceRuntime';
import type { InstanceIdentity } from './api/types';

export type { InstanceIdentity };

export const DEFAULT_INSTANCE_IDENTITY: InstanceIdentity = {
  display_name: 'frisket',
  support_contact: null,
};

let cached: Promise<InstanceIdentity> | null = null;
const instanceRuntimeApi = createInstanceRuntimeApi();

function loadInstanceIdentity(): Promise<InstanceIdentity> {
  if (!cached) {
    cached = instanceRuntimeApi.getInstanceInfo()
      .catch(() => DEFAULT_INSTANCE_IDENTITY);
  }
  return cached;
}

/** Instance branding, resolved once per page load and shared across every
 *  caller (BrandLink renders in several chrome surfaces at once). Starts at
 *  the generic default and updates in place once the fetch resolves. */
export function useInstanceIdentity(): InstanceIdentity {
  const [identity, setIdentity] = useState<InstanceIdentity>(DEFAULT_INSTANCE_IDENTITY);
  useEffect(() => {
    let active = true;
    void loadInstanceIdentity().then((value) => {
      if (active) setIdentity(value);
    });
    return () => {
      active = false;
    };
  }, []);
  return identity;
}
