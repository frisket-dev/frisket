// @vitest-environment jsdom
//
// Project General settings "Danger zone" delete flow: the delete button is
// armed only when the exact project name is typed back, the confirmed name is
// forwarded to the deletion call, success navigates to the workspace list, and
// a 409 "runs in flight" refusal is surfaced cleanly instead of deleting.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ProjectInfo } from '../../src/api/types';

const deleteProject = vi.fn();
const getProject = vi.fn();
const navigate = vi.fn();

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getProject: (...args: unknown[]) => getProject(...args),
      deleteProject: (...args: unknown[]) => deleteProject(...args),
    },
  };
});

vi.mock('../../src/routes', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/routes')>();
  return { ...actual, navigate: (...args: unknown[]) => navigate(...args) };
});

// Imported after the mocks are declared so the component picks up the stubs.
import { ProjectGeneralSettings } from '../../src/settings/SettingsSections';
import { ApiError, createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const projectApi = createProjectApi('alpha');
vi.spyOn(projectApi, 'getProject').mockImplementation(getProject);
vi.spyOn(projectApi, 'deleteProject').mockImplementation(deleteProject);
const { render } = createWorkspaceTestHarness({
  projectId: 'alpha',
  api: { projectApi: projectApi },
});

const PROJECT: ProjectInfo = {
  id: 'alpha',
  name: 'Alpha Project',
  description: '',
  sensitive: false,
  role: 'owner',
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderDangerZone() {
  getProject.mockResolvedValue(PROJECT);
  return render(<ProjectGeneralSettings project={PROJECT} />);
}

describe('ProjectGeneralSettings danger zone', () => {
  it('keeps the delete button disabled until the exact name is typed', async () => {
    renderDangerZone();
    const user = userEvent.setup();
    const button = await screen.findByTestId('project-delete-button');
    expect(button).toBeDisabled();

    const input = screen.getByTestId('project-delete-confirm-input');
    // Anti-autofill hardening: no heuristic name/id, autocomplete off.
    expect(input).toHaveAttribute('autocomplete', 'off');
    // No name attribute at all — the ConfirmTypeInput primitive's default,
    // which denies the browser any key to file form history under.
    expect(input.getAttribute('name')).toBeNull();

    await user.type(input, 'Alpha');
    expect(button).toBeDisabled();

    await user.type(input, ' Project');
    expect(button).toBeEnabled();
  });

  it('forwards the typed name to deleteProject and navigates to the picker on success', async () => {
    deleteProject.mockResolvedValue(undefined);
    renderDangerZone();
    const user = userEvent.setup();

    const input = await screen.findByTestId('project-delete-confirm-input');
    await user.type(input, 'Alpha Project');
    await user.click(screen.getByTestId('project-delete-button'));

    await waitFor(() => expect(deleteProject).toHaveBeenCalledWith('alpha', 'Alpha Project'));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ kind: 'picker' }));
  });

  it('surfaces a runs-in-flight 409 refusal cleanly and does not navigate', async () => {
    deleteProject.mockRejectedValue(new ApiError(409, 'cannot delete this project while it has runs in flight'));
    renderDangerZone();
    const user = userEvent.setup();

    const input = await screen.findByTestId('project-delete-confirm-input');
    await user.type(input, 'Alpha Project');
    await user.click(screen.getByTestId('project-delete-button'));

    expect(await screen.findByText(/runs in flight/i)).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();
  });
});
