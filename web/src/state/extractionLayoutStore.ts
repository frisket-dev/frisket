import type { SavedExtractionTemplate } from '../api/documentExtraction';

type Layout = SavedExtractionTemplate;

export function extractionLayoutDraftKey(layout: Layout): string {
  return JSON.stringify({
    draft: layout.draft,
    repeat_group_id: layout.repeat_group_id,
    reference_row_id: layout.reference_row_id,
  });
}

export interface ExtractionLayoutContext {
  retain(): void;
  release(): void;
  waitForPending(): Promise<unknown>;
  restore(serverLayout: Layout): Layout;
  rememberSaved(layout: Layout): void;
  acknowledged(): Record<number, string>;
  savedKey(layoutId: number): string | undefined;
  cacheDraft(layout: Layout): void;
  save(layout: Layout, write: () => Promise<Layout>): Promise<Layout>;
}

export interface ExtractionLayoutStoreHandle {
  readonly projectId: string;
  getContext(sheetId: string, source: string): ExtractionLayoutContext;
  dispose(): void;
}

interface InternalContext extends ExtractionLayoutContext {
  detach(): void;
}

/**
 * Owns extraction-layout drafts and write ordering for one workspace project.
 * Contexts survive view remounts while dirty or writing, then remove themselves
 * once idle. Disposing the workspace detaches them without cancelling accepted
 * writes, so provider teardown may safely run before child effect cleanup.
 */
export function createExtractionLayoutStore(projectId: string): ExtractionLayoutStoreHandle {
  const contexts = new Map<string, InternalContext>();

  const contextKey = (sheetId: string, source: string) => JSON.stringify([sheetId, source]);

  function createContext(key: string): InternalContext {
    const drafts = new Map<number, Layout>();
    const savedKeys = new Map<number, string>();
    let mounts = 0;
    let attached = true;
    let pending: Promise<Layout> | null = null;
    let latest: { key: string; work: Promise<Layout> } | null = null;

    const attach = () => {
      const existing = contexts.get(key);
      if (!existing) {
        contexts.set(key, handle);
        attached = true;
      } else {
        attached = existing === handle;
      }
    };

    const cleanIfIdle = () => {
      if (!attached || mounts > 0 || drafts.size > 0 || pending) return;
      if (contexts.get(key) === handle) contexts.delete(key);
      attached = false;
    };

    const handle: InternalContext = {
      retain() {
        mounts += 1;
        if (!attached) attach();
      },
      release() {
        mounts = Math.max(0, mounts - 1);
        cleanIfIdle();
      },
      waitForPending() {
        return pending ?? Promise.resolve();
      },
      restore(serverLayout) {
        savedKeys.set(serverLayout.id, extractionLayoutDraftKey(serverLayout));
        const draft = drafts.get(serverLayout.id);
        return draft
          ? {
              ...serverLayout,
              draft: draft.draft,
              repeat_group_id: draft.repeat_group_id,
              reference_row_id: draft.reference_row_id,
            }
          : serverLayout;
      },
      rememberSaved(layout) {
        savedKeys.set(layout.id, extractionLayoutDraftKey(layout));
      },
      acknowledged() {
        return Object.fromEntries(savedKeys);
      },
      savedKey(layoutId) {
        return savedKeys.get(layoutId);
      },
      cacheDraft(layout) {
        drafts.set(layout.id, layout);
      },
      save(layout, write) {
        const keyForDraft = extractionLayoutDraftKey(layout);
        const requestKey = `${layout.id}:${keyForDraft}`;
        if (latest?.key === requestKey && pending === latest.work) return latest.work;
        if (savedKeys.get(layout.id) === keyForDraft && !pending) {
          drafts.delete(layout.id);
          cleanIfIdle();
          return Promise.resolve(layout);
        }

        const predecessor = pending ?? Promise.resolve();
        const work = predecessor
          .catch(() => undefined)
          .then(write)
          .then((saved) => {
            savedKeys.set(layout.id, keyForDraft);
            const unsaved = drafts.get(layout.id);
            if (unsaved && extractionLayoutDraftKey(unsaved) === keyForDraft) drafts.delete(layout.id);
            return saved;
          })
          .finally(() => {
            if (pending === work) pending = null;
            if (latest?.work === work) latest = null;
            cleanIfIdle();
          });
        pending = work;
        latest = { key: requestKey, work };
        void work.catch(() => undefined);
        return work;
      },
      detach() {
        attached = false;
      },
    };
    return handle;
  }

  return {
    projectId,
    getContext(sheetId, source) {
      const key = contextKey(sheetId, source);
      let context = contexts.get(key);
      if (!context) {
        context = createContext(key);
        contexts.set(key, context);
      }
      return context;
    },
    dispose() {
      const owned = [...contexts.values()];
      contexts.clear();
      for (const context of owned) context.detach();
    },
  };
}
