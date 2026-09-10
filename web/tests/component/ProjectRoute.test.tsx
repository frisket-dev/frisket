// @vitest-environment jsdom

import { act, cleanup, screen, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ProjectInfo } from '../../src/api/open';

const listProjects = vi.hoisted(() => vi.fn<() => Promise<ProjectInfo[]>>());

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return { ...actual, listProjects };
});

vi.mock('../../src/media/pdfjsSetup', () => ({
  pdfjsLib: { getDocument: () => {}, TextLayer: class {} },
}));

import { ProjectRoute } from '../../src/App';



function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept) => {
    resolve = accept;
  });
  return { promise, resolve };
}

afterEach(() => {
  cleanup();
  listProjects.mockReset();
});

describe('ProjectRoute disjoint project-switch boundary', () => {
  it('ignores late project-A work after external navigation starts project B', async () => {
    const projectA = deferred<ProjectInfo[]>();
    const projectB = deferred<ProjectInfo[]>();
    listProjects
      .mockImplementationOnce(() => projectA.promise)
      .mockImplementationOnce(() => projectB.promise);

    const view = render(<ProjectRoute projectId="A" />);
    view.rerender(<ProjectRoute projectId="B" />);

    await act(async () => {
      projectB.resolve([]);
      await projectB.promise;
    });
    expect((await screen.findByTestId('project-route-error')).textContent).toBe(
      'No project “B” in this workspace.',
    );

    await act(async () => {
      projectA.resolve([{ id: 'A', name: 'Project A' }]);
      await projectA.promise;
    });

    expect(screen.getByTestId('project-route-error').textContent).toBe(
      'No project “B” in this workspace.',
    );
  });
});
