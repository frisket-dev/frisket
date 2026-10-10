// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ProjectInfo } from '../../src/api/open';

const { listProjects, createProject, deleteProject, seedSampleProject } = vi.hoisted(() => ({
  listProjects: vi.fn<() => Promise<ProjectInfo[]>>(),
  createProject: vi.fn<(name: string) => Promise<ProjectInfo>>(),
  deleteProject: vi.fn<() => Promise<void>>(),
  seedSampleProject: vi.fn<(projectId: string) => Promise<void>>(),
}));

vi.mock('../../src/api/open', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/open')>(),
  listProjects,
  createProject,
  deleteProject,
  seedSampleProject,
  updateProject: vi.fn(),
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
}));

import { HomeScreen } from '../../src/components/HomeScreen';

afterEach(cleanup);

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

describe('HomeScreen sample hero', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listProjects.mockResolvedValue([]);
    createProject.mockResolvedValue({ id: 'sample', name: 'Sample project' } as ProjectInfo);
    deleteProject.mockResolvedValue();
    seedSampleProject.mockResolvedValue();
  });

  it('replaces the secondary sample row only for a loaded empty Home scope', async () => {
    render(<HomeScreen onOpen={vi.fn()} />);

    await screen.findByTestId('home-sample-hero');
    expect(screen.getAllByRole('button', { name: 'Try the sample project' })).toHaveLength(1);
    expect(screen.queryByRole('button', { name: 'See the walkthroughs' })).toBeNull();
    expect(screen.queryByTestId('home-filter')).toBeNull();
    expect(screen.queryByTestId('home-empty')).toBeNull();

    fireEvent.click(screen.getByTestId('home-nav-starred'));
    expect(screen.queryByTestId('home-sample-hero')).toBeNull();
    expect(screen.getByTestId('home-filter')).toBeInTheDocument();
    expect(screen.getByTestId('try-sample-project')).toBeInTheDocument();
  });

  it('opens the seeded sample with the requested guide intent', async () => {
    const onOpen = vi.fn();
    render(<HomeScreen onOpen={onOpen} />);

    await screen.findByTestId('home-sample-hero');
    fireEvent.click(screen.getByRole('button', { name: 'Try the sample project' }));

    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(
      { id: 'sample', name: 'Sample project' },
      { openGuide: true },
    ));
    expect(seedSampleProject).toHaveBeenCalledWith('sample');
  });

  it('prevents repeat clicks while sample setup is in progress', async () => {
    const seed = deferred<void>();
    seedSampleProject.mockReturnValue(seed.promise);
    const onOpen = vi.fn();
    render(<HomeScreen onOpen={onOpen} />);

    await screen.findByTestId('home-sample-hero');
    const button = screen.getByRole('button', { name: 'Try the sample project' });
    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => expect(seedSampleProject).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('button', { name: /Setting up the sample/ })).toBeDisabled();

    seed.resolve();
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(
      { id: 'sample', name: 'Sample project' },
      { openGuide: true },
    ));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it('reopens the existing sample with walkthroughs from the populated home shortcut', async () => {
    const project = { id: 'sample', name: 'Sample project' } as ProjectInfo;
    listProjects.mockResolvedValue([project]);
    const onOpen = vi.fn();
    render(<HomeScreen onOpen={onOpen} />);

    fireEvent.click(await screen.findByRole('button', { name: 'Try the sample project' }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(project, { openGuide: true }));
    expect(createProject).not.toHaveBeenCalled();
    expect(seedSampleProject).toHaveBeenCalledWith('sample');
  });
});
