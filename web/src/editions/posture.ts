export const OPEN_EDITION_POSTURES = ['local', 'team'] as const;

/** Opaque browser-composition identity owned by the mounted edition. */
export type EditionPosture = string;

export interface PostureCapabilities {
  configurableNotificationDestinations: boolean;
  configurableNotificationEmail: boolean;
  identity: boolean;
  team: boolean;
}

export interface EditionDescriptor {
  id: EditionPosture;
  capabilities: Readonly<PostureCapabilities>;
}

export function immutableEditionDescriptor(
  descriptor: EditionDescriptor,
): Readonly<EditionDescriptor> {
  const id = descriptor.id.trim();
  if (!id) throw new Error('Edition descriptor id must not be empty');
  return Object.freeze({
    id,
    capabilities: Object.freeze({ ...descriptor.capabilities }),
  });
}

export const LOCAL_EDITION_DESCRIPTOR = immutableEditionDescriptor({
  id: 'local',
  capabilities: {
    configurableNotificationDestinations: true,
    configurableNotificationEmail: true,
    identity: false,
    team: false,
  },
});

export const TEAM_EDITION_DESCRIPTOR = immutableEditionDescriptor({
  id: 'team',
  capabilities: {
    configurableNotificationDestinations: true,
    configurableNotificationEmail: true,
    identity: true,
    team: true,
  },
});
