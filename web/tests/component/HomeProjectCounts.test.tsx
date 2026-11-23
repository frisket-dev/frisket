// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ProjectInfo } from '../../src/api/open';
import { runtimeConfig } from '../support/domainFixtures';

const { listProjects, fetchProjectSheetCounts } = vi.hoisted(() => ({
  listProjects: vi.fn<() => Promise<ProjectInfo[]>>(),
  fetchProjectSheetCounts: vi.fn<(projectId: string, options: { signal?: AbortSignal }) => Promise<{ sheets: number; rows: number }>>(),
}));

vi.mock('../../src/api/open', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/open')>(),
  listProjects,
  getRuntimeConfig: () => Promise.resolve(runtimeConfig()),
}));

vi.mock('../../src/api/homeSheetCounts', () => ({ fetchProjectSheetCounts }));

vi.mock('../../src/shellIdentity', () => ({
  useShellIdentity: () => ({ identityMode: false, me: null, resolved: true }),
}));

vi.mock('../../src/instanceIdentity', () => ({
  useInstanceIdentity: () => ({ display_name: 'frisket', support_contact: null }),
}));

import { HomeScreen } from '../../src/components/HomeScreen';

afterEach(cleanup);

describe('Home project counts', () => {
  it('asks for a few counts at a time and cancels the rest when Home closes', async () => {
    const projects = Array.from({ length: 10 }, (_, index) => ({ id: `p${index}`, name: `Project ${index}` }));
    listProjects.mockResolvedValue(projects);
    const pending: Array<{ signal?: AbortSignal; resolve(): void }> = [];
    fetchProjectSheetCounts.mockImplementation((_projectId, options) => new Promise((resolve) => {
      pending.push({ signal: options.signal, resolve: () => resolve({ sheets: 1, rows: 2 }) });
    }));

    const view = render(<HomeScreen onOpen={vi.fn()} />);
    await screen.findByTestId('project-list');
    await waitFor(() => expect(fetchProjectSheetCounts).toHaveBeenCalledTimes(4));

    pending[0].resolve();
    await waitFor(() => expect(fetchProjectSheetCounts).toHaveBeenCalledTimes(5));
    expect(fetchProjectSheetCounts.mock.calls.map(([id]) => id)).toEqual(['p0', 'p1', 'p2', 'p3', 'p4']);

    view.unmount();
    expect(pending.slice(1).every((request) => request.signal?.aborted)).toBe(true);
  });
});
