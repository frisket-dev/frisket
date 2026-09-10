// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { TeamAdminOverview } from '../../src/components/AdminPage';



afterEach(cleanup);

describe('TeamAdminOverview', () => {
  it('renders the team endpoint totals without commercial or multi-org copy', () => {
    render(
      <TeamAdminOverview
        overview={{
          totals: { orgs: 1, users: 7, projects: 3, pending_invites: 2 },
        }}
      />,
    );

    const overview = screen.getByTestId('team-admin-overview');
    expect(overview).toHaveTextContent('Members');
    expect(overview).toHaveTextContent('7');
    expect(overview).toHaveTextContent('Projects');
    expect(overview).toHaveTextContent('3');
    expect(overview).toHaveTextContent('Pending invites');
    expect(overview).toHaveTextContent('2');
    expect(overview).not.toHaveTextContent(/\b(?:spend|credits|organizations)\b/i);
  });
});
