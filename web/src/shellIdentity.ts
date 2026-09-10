// Shell identity + tier detection. The shell menus (account menu, Home top bar)
// need to know the instance tier so the TIER HONESTY RULE holds: the local tier
// has no /api/me (404) and therefore renders
// NO teams / sharing / sign-out chrome. This mirrors the App.tsx boot probe
// (/api/me: 200|401 → hosted, anything else → local) but as a shared, cached
// hook so any shell surface can read it without threading a prop down.
//
// Cached once per page load (like instanceIdentity): a hosted deployment's tier
// never changes mid-session, and a shared cache keeps the avatar in the chrome
// bar and the Home top bar from each firing their own probe.

import { useEffect, useState } from 'react';
import type { MeInfo } from './api/open';
import { getMe } from './api/open';
import { useEditionModule } from './editions/module';
import type { EditionDescriptor } from './editions/posture';

export interface ShellIdentity {
  /** True on the hosted/auth tier (a real signed-in identity exists); false on
   *  the local tier. Gates every tier-scoped affordance (Sign out, teams,
   *  sharing). */
  identityMode: boolean;
  /** The signed-in identity when one exists; null on the local tier. */
  me: MeInfo | null;
  resolved: boolean;
}

const LOCAL_IDENTITY: ShellIdentity = { identityMode: false, me: null, resolved: true };

function initialIdentity(identityMode: boolean): ShellIdentity {
  return { identityMode, me: null, resolved: !identityMode };
}

const cached = new WeakMap<Readonly<EditionDescriptor>, Promise<ShellIdentity>>();

function loadShellIdentity(
  descriptor: Readonly<EditionDescriptor>,
): Promise<ShellIdentity> {
  let pending = cached.get(descriptor);
  if (!pending) {
    const identityMode = descriptor.capabilities.identity;
    pending = identityMode
      ? getMe()
          .then((me: MeInfo) => ({ identityMode: true, me, resolved: true }))
          .catch(() => ({ identityMode: true, me: null, resolved: true }))
      : Promise.resolve(LOCAL_IDENTITY);
    cached.set(descriptor, pending);
  }
  return pending;
}

/** The shell's tier + identity, resolved once per page load and shared across
 *  every caller. Starts local (the safe, chrome-free default) and updates in
 *  place once the probe resolves. */
export function useShellIdentity(): ShellIdentity {
  const { descriptor } = useEditionModule();
  const [identity, setIdentity] = useState<ShellIdentity>(
    () => initialIdentity(descriptor.capabilities.identity),
  );
  useEffect(() => {
    let active = true;
    void loadShellIdentity(descriptor).then((value) => {
      if (active) setIdentity(value);
    });
    return () => {
      active = false;
    };
  }, [descriptor]);
  return identity;
}
