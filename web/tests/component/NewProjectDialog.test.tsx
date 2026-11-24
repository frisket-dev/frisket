// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ProjectInfo, RuntimeConfig } from '../../src/api/open';
import type { CreateProjectInput } from '../../src/api/httpContractRoutes/projects';
import { runtimeConfig } from '../support/domainFixtures';

const { listProjects, createProject, getRuntimeConfig, sharesUsage, navigate } = vi.hoisted(() => ({
  listProjects: vi.fn<() => Promise<ProjectInfo[]>>(),
  createProject: vi.fn<(input: CreateProjectInput) => Promise<ProjectInfo>>(),
  getRuntimeConfig: vi.fn<() => Promise<RuntimeConfig>>(),
  sharesUsage: vi.fn<() => boolean>(),
  navigate: vi.fn(),
}));

vi.mock('../../src/api/open', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/open')>(),
  listProjects,
  createProject,
  getRuntimeConfig,
  deleteProject: vi.fn(),
  seedSampleProject: vi.fn(),
  updateProject: vi.fn(),
}));

vi.mock('../../src/routes', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/routes')>(),
  navigate,
}));

vi.mock('../../src/shellIdentity', () => ({
  useShellIdentity: () => ({ identityMode: false, me: null, resolved: true }),
}));

vi.mock('../../src/instanceIdentity', () => ({
  useInstanceIdentity: () => ({ display_name: 'frisket', support_contact: null }),
}));

vi.mock('../../src/telemetry/productTelemetry', () => ({
  durationBucket: () => 'fast',
  failureCategory: () => 'unknown',
  sendProductTelemetry: vi.fn(),
  workspaceSharesUsageData: sharesUsage,
}));

vi.mock('../../src/api/homeSheetCounts', () => ({
  fetchProjectSheetCounts: () => Promise.resolve({ sheets: 0, rows: 0 }),
}));

import { HomeScreen } from '../../src/components/HomeScreen';
import { projectCover } from '../../src/components/projectCover';

afterEach(cleanup);

const created: ProjectInfo = { id: 'p-new', name: 'School board records' };

async function openDialog(onOpen = vi.fn()) {
  render(<HomeScreen onOpen={onOpen} />);
  await screen.findByTestId('project-list');
  const trigger = screen.getByTestId('home-new-project');
  trigger.focus();
  fireEvent.click(trigger);
  const dialog = await screen.findByTestId('new-project-dialog');
  return { dialog, trigger, onOpen };
}

describe('New project dialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listProjects.mockResolvedValue([{ id: 'existing', name: 'Existing' }]);
    createProject.mockResolvedValue(created);
    getRuntimeConfig.mockResolvedValue(runtimeConfig());
    sharesUsage.mockReturnValue(true);
  });

  it('opens with the name field focused and the defaults selected', async () => {
    await openDialog();
    expect(screen.getByTestId('new-project-name')).toHaveFocus();
    expect(screen.getByRole('radiogroup', { name: 'Allowed services' })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /All services/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: "Don't share usage data from this project" })).not.toBeChecked();
    expect(screen.queryByTestId('allowed-services-disclaimer')).toBeNull();
    expect(screen.queryByTestId('new-project-preview-services')).toBeNull();
    expect(screen.getByTestId('new-project-preview-title')).toHaveTextContent('Untitled project');
  });

  it.each([
    ['this_machine_only', /This machine only/],
    ['approved_services', /Approved services/],
    ['all_services', /All services/],
  ] as const)('creates with %s and an untouched checkbox as inherit', async (value, label) => {
    const { onOpen } = await openDialog();
    fireEvent.change(screen.getByTestId('new-project-name'), { target: { value: '  School board records ' } });
    fireEvent.click(screen.getByRole('radio', { name: label }));
    fireEvent.click(screen.getByTestId('create-project'));
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(created));
    expect(createProject).toHaveBeenCalledWith({
      name: 'School board records',
      allowed_services: value,
      usage_data_sharing: 'inherit',
      cover_seed: expect.stringMatching(/^[a-z0-9]{10}$/),
    });
  });

  it('sends off or on only when the checkbox moved from the workspace setting', async () => {
    await openDialog();
    const box = screen.getByRole('checkbox', { name: "Don't share usage data from this project" });
    fireEvent.click(box);
    fireEvent.click(screen.getByTestId('create-project'));
    await waitFor(() => expect(createProject).toHaveBeenCalledTimes(1));
    expect(createProject.mock.calls[0][0].usage_data_sharing).toBe('off');
    cleanup();

    sharesUsage.mockReturnValue(false);
    await openDialog();
    const checkedBox = screen.getByRole('checkbox', { name: "Don't share usage data from this project" });
    expect(checkedBox).toBeChecked();
    fireEvent.click(checkedBox);
    fireEvent.click(screen.getByTestId('create-project'));
    await waitFor(() => expect(createProject).toHaveBeenCalledTimes(2));
    expect(createProject.mock.calls[1][0].usage_data_sharing).toBe('on');
    cleanup();

    await openDialog();
    const toggled = screen.getByRole('checkbox', { name: "Don't share usage data from this project" });
    fireEvent.click(toggled);
    fireEvent.click(toggled);
    fireEvent.click(screen.getByTestId('create-project'));
    await waitFor(() => expect(createProject).toHaveBeenCalledTimes(3));
    expect(createProject.mock.calls[2][0].usage_data_sharing).toBe('inherit');
  });

  it('never changes the checkbox when a restricted option is chosen', async () => {
    await openDialog();
    const box = screen.getByRole('checkbox', { name: "Don't share usage data from this project" });
    fireEvent.click(screen.getByRole('radio', { name: /This machine only/ }));
    expect(box).not.toBeChecked();
    fireEvent.click(screen.getByRole('radio', { name: /Approved services/ }));
    expect(box).not.toBeChecked();
  });

  it('creates on Enter in the name field, with an empty name left to the server', async () => {
    await openDialog();
    const name = screen.getByTestId('new-project-name');
    fireEvent.submit(name.closest('form')!);
    await waitFor(() => expect(createProject).toHaveBeenCalledWith({
      name: '',
      allowed_services: 'all_services',
      usage_data_sharing: 'inherit',
      cover_seed: expect.stringMatching(/^[a-z0-9]{10}$/),
    }));
  });

  it('shows the pill, disclaimer and endpoints link for the restricted options', async () => {
    await openDialog();
    fireEvent.change(screen.getByTestId('new-project-name'), { target: { value: 'School board records' } });
    expect(screen.getByTestId('new-project-preview-title')).toHaveTextContent('School board records');

    fireEvent.click(screen.getByRole('radio', { name: /This machine only/ }));
    expect(screen.getByTestId('new-project-preview-services')).toHaveTextContent('This machine only');
    expect(screen.getByTestId('allowed-services-disclaimer')).toHaveTextContent('Best-effort. Not network isolation.');
    expect(screen.queryByTestId('manage-approved-endpoints')).toBeNull();

    fireEvent.click(screen.getByRole('radio', { name: /Approved services/ }));
    expect(screen.getByTestId('new-project-preview-services')).toHaveTextContent('Approved services');
    fireEvent.click(screen.getByTestId('manage-approved-endpoints'));
    expect(navigate).toHaveBeenCalledWith({ kind: 'settings', scope: 'personal', section: 'approved-endpoints' });
  });

  it('re-rolls the preview cover and creates the project with the cover it shows', async () => {
    await openDialog();
    const cover = () => screen.getByTestId('new-project-preview').querySelector<HTMLElement>('.home-card-cover');
    const first = `${cover()?.style.background}|${cover()?.textContent}`;
    fireEvent.click(screen.getByTestId('new-project-different-cover'));
    const shown = cover()?.style.background;
    const shownGlyph = cover()?.querySelector('.home-card-glyph')?.textContent;
    expect(shown).toMatch(/^linear-gradient/);
    expect(`${shown}|${cover()?.textContent}`).not.toBe(first);
    fireEvent.click(screen.getByTestId('create-project'));
    await waitFor(() => expect(createProject).toHaveBeenCalledTimes(1));
    const seed = createProject.mock.calls[0][0].cover_seed;
    expect(seed).toBeDefined();
    cleanup();

    listProjects.mockResolvedValue([{ ...created, cover_seed: seed }]);
    render(<HomeScreen onOpen={vi.fn()} />);
    const card = await screen.findByTestId(`project-${created.id}`);
    const cardCover = card.querySelector<HTMLElement>('.home-card-cover');
    expect(cardCover?.style.background).toBe(shown);
    expect(cardCover?.querySelector('.home-card-glyph')?.textContent).toBe(shownGlyph);
  });

  it('starts from the organization default in /api/config', async () => {
    getRuntimeConfig.mockResolvedValue(runtimeConfig({ allowed_services_default: 'approved_services' }));
    render(<HomeScreen onOpen={vi.fn()} />);
    await screen.findByTestId('project-list');
    await waitFor(() => expect(getRuntimeConfig).toHaveBeenCalled());
    await Promise.resolve();
    fireEvent.click(screen.getByTestId('home-new-project'));
    expect(await screen.findByRole('radio', { name: /Approved services/ })).toBeChecked();
    expect(screen.getByTestId('new-project-preview-services')).toHaveTextContent('Approved services');
    fireEvent.click(screen.getByTestId('create-project'));
    await waitFor(() => expect(createProject).toHaveBeenCalledWith(expect.objectContaining({
      allowed_services: 'approved_services',
    })));
  });

  it('starts from All services when /api/config has no default', async () => {
    getRuntimeConfig.mockResolvedValue(runtimeConfig({ allowed_services_default: undefined }));
    await openDialog();
    expect(screen.getByRole('radio', { name: /All services/ })).toBeChecked();
  });

  it('hides an option the edition does not offer', async () => {
    getRuntimeConfig.mockResolvedValue(runtimeConfig({ allowed_services_offered: ['approved_services', 'all_services'] }));
    await openDialog();
    await waitFor(() => expect(screen.getByRole('radio', { name: /Approved services/ })).toBeInTheDocument());
    expect(screen.queryByRole('radio', { name: /This machine only/ })).toBeNull();
  });

  it.each([
    ['Cancel', () => fireEvent.click(screen.getByTestId('new-project-cancel'))],
    ['the close button', () => fireEvent.click(screen.getByRole('button', { name: 'Close' }))],
    ['Escape', () => fireEvent(screen.getByTestId('new-project-dialog'), new Event('cancel', { cancelable: true }))],
  ])('closes on %s without creating and returns focus to the trigger', async (_label, close) => {
    const { trigger } = await openDialog();
    close();
    await waitFor(() => expect(screen.queryByTestId('new-project-dialog')).toBeNull());
    expect(createProject).not.toHaveBeenCalled();
    expect(trigger).toHaveFocus();
  });

  it('keeps the dialog open with the error when creation fails', async () => {
    createProject.mockRejectedValue(new Error('disk full'));
    await openDialog();
    fireEvent.click(screen.getByTestId('create-project'));
    expect(await screen.findByTestId('new-project-error')).toHaveTextContent('disk full');
    expect(screen.getByTestId('new-project-dialog')).toBeInTheDocument();
  });
});

describe('Project cover', () => {
  it('derives a project without a seed from its id exactly as before seeds existed', () => {
    expect(projectCover({ id: 'council-minutes-2026' })).toEqual({
      style: { background: 'linear-gradient(135deg, hsl(324 42% 88%), hsl(4 46% 80%))' },
      glyph: '¶',
    });
    expect(projectCover({ id: 'council-minutes-2026', cover_seed: null }))
      .toEqual(projectCover({ id: 'council-minutes-2026' }));
  });
});

describe('Home card', () => {
  it('shows the Allowed services pill only for a restricted project', async () => {
    vi.clearAllMocks();
    getRuntimeConfig.mockResolvedValue(runtimeConfig());
    listProjects.mockResolvedValue([
      { id: 'open', name: 'Open', allowed_services: 'all_services' },
      { id: 'local', name: 'Local', allowed_services: 'this_machine_only' },
    ]);
    render(<HomeScreen onOpen={vi.fn()} />);
    expect(await screen.findByTestId('project-local-services')).toHaveTextContent('This machine only');
    expect(screen.queryByTestId('project-open-services')).toBeNull();
  });
});
