import type { ActionCatalogPayload } from './types';

export interface ActionCatalogLoaders {
  loadGlobal: () => Promise<ActionCatalogPayload>;
  loadProject: (projectId: string) => Promise<ActionCatalogPayload>;
  emitInvalidated: () => void;
}

/** Owns signal-free, per-API action-catalog request sharing and invalidation. */
export class ActionCatalogCache {
  private readonly promises = new Map<string, Promise<ActionCatalogPayload>>();
  private readonly loaders: ActionCatalogLoaders;

  constructor(loaders: ActionCatalogLoaders) {
    this.loaders = loaders;
  }

  load(projectId: string | null): Promise<ActionCatalogPayload> {
    const cacheKey = projectId ?? '<global>';
    const cached = this.promises.get(cacheKey);
    if (cached) return cached;

    const loader = projectId === null ? this.loaders.loadGlobal : () => this.loaders.loadProject(projectId);
    const loaderPromise = loader();
    const pending = loaderPromise.catch((error: unknown) => {
      if (this.promises.get(cacheKey) === pending) {
        this.promises.delete(cacheKey);
      }
      throw error;
    });
    this.promises.set(cacheKey, pending);
    return pending;
  }

  invalidate(): void {
    this.promises.clear();
    this.loaders.emitInvalidated();
  }
}
