import { describe, expect, it, vi } from 'vitest';

import { ActionCatalogCache } from '../../src/api/actionCatalog';
import type { ActionCatalogPayload } from '../../src/api/types';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function createCache(options: {
  global?: () => Promise<ActionCatalogPayload>;
  project?: (projectId: string) => Promise<ActionCatalogPayload>;
} = {}) {
  const emitInvalidated = vi.fn();
  const global = vi.fn(options.global ?? (async () => ({}) as ActionCatalogPayload));
  const project = vi.fn(
    options.project ?? (async () => ({}) as ActionCatalogPayload),
  );
  return {
    cache: new ActionCatalogCache({
      loadGlobal: global,
      loadProject: project,
      emitInvalidated,
    }),
    emitInvalidated,
    global,
    project,
  };
}

describe('ActionCatalogCache', () => {
  it('coalesces concurrent requests for the same raw project key', () => {
    const pending = deferred<ActionCatalogPayload>();
    const { cache, project } = createCache({ project: () => pending.promise });

    const first = cache.load('project/a%2Fb');
    const second = cache.load('project/a%2Fb');

    expect(second).toBe(first);
    expect(project).toHaveBeenCalledTimes(1);
    expect(project).toHaveBeenCalledWith('project/a%2Fb');
  });

  it('keeps global and distinct raw project keys separate', () => {
    const { cache, global, project } = createCache();

    const globalCatalog = cache.load(null);
    const emptyProject = cache.load('');
    const projectA = cache.load('a');
    const projectB = cache.load('b');

    expect(globalCatalog).not.toBe(emptyProject);
    expect(emptyProject).not.toBe(projectA);
    expect(projectA).not.toBe(projectB);
    expect(global).toHaveBeenCalledTimes(1);
    expect(project).toHaveBeenNthCalledWith(1, '');
    expect(project).toHaveBeenNthCalledWith(2, 'a');
    expect(project).toHaveBeenNthCalledWith(3, 'b');
  });

  it('evicts only a rejected key so a later request retries it', async () => {
    const project = vi
      .fn<() => Promise<ActionCatalogPayload>>()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({} as ActionCatalogPayload);
    const { cache } = createCache({ project });

    await expect(cache.load('retry-me')).rejects.toThrow('offline');
    await cache.load('retry-me');

    expect(project).toHaveBeenCalledTimes(2);
  });

  it('keeps an invalidation successor after an older request rejects', async () => {
    const oldRequest = deferred<ActionCatalogPayload>();
    const successor = deferred<ActionCatalogPayload>();
    const { cache, project } = createCache();
    project.mockReturnValueOnce(oldRequest.promise).mockReturnValueOnce(successor.promise);

    const oldCatalog = cache.load('project-a');
    cache.invalidate();
    const replacement = cache.load('project-a');
    oldRequest.reject(new Error('offline'));

    await expect(oldCatalog).rejects.toThrow('offline');

    expect(cache.load('project-a')).toBe(replacement);
    expect(project).toHaveBeenCalledTimes(2);

    successor.resolve({} as ActionCatalogPayload);
    await replacement;
  });

  it('clears every cached key, emits once, and reloads after invalidation', async () => {
    const { cache, emitInvalidated, global, project } = createCache();

    await Promise.all([cache.load(null), cache.load('project-a'), cache.load('project-b')]);
    cache.invalidate();
    await Promise.all([cache.load(null), cache.load('project-a'), cache.load('project-b')]);

    expect(emitInvalidated).toHaveBeenCalledTimes(1);
    expect(global).toHaveBeenCalledTimes(2);
    expect(project).toHaveBeenCalledTimes(4);
  });
});
