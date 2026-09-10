import type { ReactNode } from 'react';

import {
  AuthScreen,
  OpenEditionRoot,
  defineEditionModule,
  getAdminUsers,
  mountEdition,
  useShellIdentity,
  type AdminUsers,
  type EditionRoute,
  type EditionSettingsSection,
  type ShellIdentity,
  type WatchControlSlotProps,
} from '@frisket/frontend-host';

const publicRoute = (): ReactNode => (
  <AuthScreen heading="Synthetic public route" headingId="synthetic-public-route">
    Public
  </AuthScreen>
);

const routes: readonly EditionRoute[] = [
  {
    id: 'synthetic-public',
    path: '/synthetic-public',
    access: 'public',
    handler: publicRoute,
  },
  {
    id: 'synthetic-authenticated',
    path: '/synthetic-authenticated',
    access: 'authenticated',
    handler: () => <main>Authenticated</main>,
  },
];

const settingsSections: readonly EditionSettingsSection[] = [{
  id: 'organization.synthetic',
  scope: 'organization',
  section: 'synthetic',
  routePattern: '/settings/organization/synthetic',
  title: 'Synthetic',
  navGroup: 'Organization',
  searchLabels: ['synthetic'],
  visibility: 'hosted-only',
  permission: 'owner',
  component: 'organization.synthetic',
  summary: 'Packed-host consumer proof.',
  handler: () => <section>Packed settings section</section>,
}];

const loadAdminUsers = (): Promise<AdminUsers> => getAdminUsers();

export function EditionAnalyticsEffect(): null {
  const identity: ShellIdentity = useShellIdentity();
  void identity;
  return null;
}

const edition = defineEditionModule({
  descriptor: {
    id: 'synthetic',
    capabilities: {
      configurableNotificationDestinations: false,
      configurableNotificationEmail: false,
      identity: true,
      team: true,
    },
  },
  routes,
  settingsSections,
  WatchControlSlot: ({
    projectId,
    watch,
  }: WatchControlSlotProps): ReactNode => (
    <button
      type="button"
      data-project-id={projectId}
      onClick={() => void loadAdminUsers()}
    >
      Configure {watch.name} for {projectId}
    </button>
  ),
});

mountEdition(edition, (
  <>
    <EditionAnalyticsEffect />
    <OpenEditionRoot />
  </>
));
