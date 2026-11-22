// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  listProjectMembers: vi.fn(),
  listProjectInvites: vi.fn(),
  setProjectMember: vi.fn(),
  removeProjectMember: vi.fn(),
  createProjectInvite: vi.fn(),
  revokeProjectInvite: vi.fn(),
}));

vi.mock('../../src/api/open', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/open')>(),
  ...api,
}));

import { ApiError } from '../../src/api/open';
import type { ProjectInfo, ProjectInvite, ProjectMember } from '../../src/api/types';
import { ProjectAccessSettings } from '../../src/settings/ProjectSettings';

const OWNER_PROJECT: ProjectInfo = {
  id: 'alpha',
  name: 'Alpha Project',
  description: '',
  sensitive: false,
  role: 'owner',
};

const VIEWER_PROJECT: ProjectInfo = {
  ...OWNER_PROJECT,
  role: 'viewer',
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('ProjectAccessSettings', () => {
  it('updates member roles and creates project invites for owners', async () => {
    let members: ProjectMember[] = [
      { user_id: 1, email: 'owner@example.org', role: 'owner' },
      { user_id: 2, email: 'reporter@example.org', role: 'editor' },
    ];
    let invites: ProjectInvite[] = [
      {
        id: 50,
        project_id: 'alpha',
        email: 'pending@example.org',
        role: 'viewer',
        created_at: '2026-06-17T12:00:00Z',
        expires_at: '2026-07-01T12:00:00Z',
        accepted_at: null,
        revoked_at: null,
      },
    ];
    api.listProjectMembers.mockImplementation(async () => members.map((item) => ({ ...item })));
    api.listProjectInvites.mockImplementation(async () => invites.map((item) => ({ ...item })));
    api.setProjectMember.mockImplementation(async (_projectId: string, email: string, role: ProjectMember['role']) => {
      members = members.map((item) => (item.email === email ? { ...item, role } : item));
      return { ok: true, email, role };
    });
    api.createProjectInvite.mockImplementation(async (_projectId: string, email: string, role: 'viewer' | 'editor') => {
      const created: ProjectInvite = {
        id: 51,
        project_id: 'alpha',
        email,
        role,
        created_at: '2026-06-17T13:00:00Z',
        expires_at: '2026-07-01T13:00:00Z',
        accepted_at: null,
        revoked_at: null,
      };
      invites = [...invites, created];
      return { sent: true, invite: created };
    });

    const user = userEvent.setup();
    render(<ProjectAccessSettings project={OWNER_PROJECT} identityMode />);

    const section = await screen.findByTestId('project-access-settings');
    await waitFor(() => expect(section).toHaveTextContent('reporter@example.org'));
    expect(section).toHaveTextContent('owner@example.org');

    await user.selectOptions(screen.getByTestId('project-member-role-2'), 'viewer');
    await waitFor(() => expect(api.setProjectMember).toHaveBeenCalledWith('alpha', 'reporter@example.org', 'viewer'));

    await user.type(screen.getByTestId('project-invite-email'), 'collab@example.org');
    await user.click(screen.getByTestId('project-invite-submit'));
    await waitFor(() => expect(api.createProjectInvite).toHaveBeenCalledWith('alpha', 'collab@example.org', 'viewer'));
    await waitFor(() => expect(section).toHaveTextContent('collab@example.org'));
  });

  it('renders partial member data and read-only controls when invites return 403 for viewers', async () => {
    api.listProjectMembers.mockResolvedValue([
      { user_id: 1, email: 'owner@example.org', role: 'owner' },
      { user_id: 2, email: 'viewer@example.org', role: 'viewer' },
    ]);
    api.listProjectInvites.mockRejectedValue(new ApiError(403, 'permission denied'));

    render(<ProjectAccessSettings project={VIEWER_PROJECT} identityMode />);

    const section = await screen.findByTestId('project-access-settings');
    await waitFor(() => expect(section).toHaveTextContent('viewer@example.org'));
    expect(section).toHaveTextContent('owner@example.org');
    expect(section).toHaveTextContent('cannot change members or invites');
    expect(section).toHaveTextContent('You do not have permission to manage project invites.');
    expect(screen.getByTestId('project-member-role-2')).toBeDisabled();
    expect(screen.getByTestId('project-member-remove-2')).toBeDisabled();
    expect(screen.queryByTestId('project-invite-email')).not.toBeInTheDocument();
  });
});
