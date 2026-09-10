import { describe, expect, it } from 'vitest';

import {
  LOCAL_EDITION_DESCRIPTOR,
  OPEN_EDITION_POSTURES,
  TEAM_EDITION_DESCRIPTOR,
  immutableEditionDescriptor,
} from '../../src/editions/posture';

describe('edition descriptors', () => {
  it('base owns only local and team', () => {
    expect(OPEN_EDITION_POSTURES).toEqual(['local', 'team']);
    expect(LOCAL_EDITION_DESCRIPTOR.capabilities).toEqual({
      configurableNotificationDestinations: true,
      configurableNotificationEmail: true,
      identity: false,
      team: false,
    });
    expect(TEAM_EDITION_DESCRIPTOR.capabilities).toEqual({
      configurableNotificationDestinations: true,
      configurableNotificationEmail: true,
      identity: true,
      team: true,
    });
  });

  it('freezes an opaque downstream descriptor without registering global state', () => {
    const descriptor = immutableEditionDescriptor({
      id: 'synthetic-customer',
      capabilities: {
        configurableNotificationDestinations: false,
        configurableNotificationEmail: false,
        identity: true,
        team: true,
      },
    });
    expect(descriptor.capabilities).toEqual({
      configurableNotificationDestinations: false,
      configurableNotificationEmail: false,
      identity: true,
      team: true,
    });
    expect(Object.isFrozen(descriptor)).toBe(true);
    expect(Object.isFrozen(descriptor.capabilities)).toBe(true);
    expect(() => immutableEditionDescriptor({
      id: '  ',
      capabilities: descriptor.capabilities,
    })).toThrow('must not be empty');
  });
});
