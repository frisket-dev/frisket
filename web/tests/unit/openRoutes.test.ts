import { describe, expect, it } from 'vitest';

import { routesFor } from '../../src/routes/openRoutes';
import {
  LOCAL_EDITION_DESCRIPTOR,
  TEAM_EDITION_DESCRIPTOR,
} from '../../src/editions/posture';

describe('edition route contributions', () => {
  it('declares access for the built-in Local and Team routes', () => {
    expect(routesFor(LOCAL_EDITION_DESCRIPTOR).map(({ id, access }) => [id, access])).toEqual([
      ['workspace', 'authenticated'],
      ['settings', 'authenticated'],
    ]);
    expect(routesFor(TEAM_EDITION_DESCRIPTOR).map(({ id, access }) => [id, access])).toEqual([
      ['workspace', 'authenticated'],
      ['settings', 'authenticated'],
      ['sign-in', 'public'],
      ['admin-users', 'authenticated'],
      ['admin-jobs', 'authenticated'],
      ['admin-audit', 'authenticated'],
      ['admin-errors', 'authenticated'],
      ['admin-diagnostics', 'authenticated'],
    ]);
  });

  it('requires discoverable links to bring their own presentation', () => {
    expect(() => routesFor(TEAM_EDITION_DESCRIPTOR, [{
        id: 'request-membership',
        path: '/request-membership',
        access: 'public',
        discoverableFrom: 'sign-in',
      }])).toThrow('missing linkLabel');
  });

  it('carries a route-supplied sign-in label without a product fallback', () => {
    const routes = routesFor(TEAM_EDITION_DESCRIPTOR, [{
        id: 'request-membership',
        path: '/request-membership',
        access: 'public',
        discoverableFrom: 'sign-in',
        linkLabel: 'Ask this operator for membership',
      }]);

    expect(routes.find((route) => route.id === 'request-membership')?.linkLabel).toBe(
      'Ask this operator for membership',
    );
  });
});
