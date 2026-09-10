import type { EditionDescriptor } from '../editions/posture';
import type { ComponentType, ReactNode } from 'react';

export interface EditionRoute {
  readonly id: string;
  readonly path: string;
  /** The shared root enforces this before invoking the route contribution. */
  readonly access: 'public' | 'authenticated';
  readonly component?: ComponentType;
  readonly handler?: () => ReactNode;
  readonly discoverableFrom?: string;
  readonly stableHook?: string;
  /** Required presentation for a discoverable link; the host route owns it. */
  readonly linkLabel?: string;
}

const LOCAL_ROUTES: readonly EditionRoute[] = [
  { id: 'workspace', path: '/', access: 'authenticated', handler: () => null },
  { id: 'settings', path: '/settings', access: 'authenticated', handler: () => null },
];

const TEAM_ROUTES: readonly EditionRoute[] = [
  { id: 'sign-in', path: '/sign-in', access: 'public', handler: () => null },
  { id: 'admin-users', path: '/admin/users', access: 'authenticated' },
  { id: 'admin-jobs', path: '/admin/jobs', access: 'authenticated' },
  { id: 'admin-audit', path: '/admin/audit', access: 'authenticated' },
  { id: 'admin-errors', path: '/admin/errors', access: 'authenticated' },
  { id: 'admin-diagnostics', path: '/admin/diagnostics', access: 'authenticated' },
];

export function routesFor(
  edition: Readonly<EditionDescriptor>,
  contributed: readonly EditionRoute[] = [],
): EditionRoute[] {
  const capabilities = edition.capabilities;
  const open = capabilities.identity ? [...LOCAL_ROUTES, ...TEAM_ROUTES] : [...LOCAL_ROUTES];
  for (const route of contributed) {
    if (route.discoverableFrom && !route.linkLabel?.trim()) {
      throw new Error(`Discoverable edition route is missing linkLabel: ${route.id}`);
    }
  }
  return [
    ...open,
    ...contributed,
  ];
}
