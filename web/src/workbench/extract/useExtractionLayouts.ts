import { useCallback, useEffect, useRef, useState } from 'react';
import { documentExtractionApi, type SavedExtractionTemplate } from '../../api/documentExtraction';

export const EMPTY_EXTRACTION_DRAFT = {
  reference_blob_id: '', reference_fingerprint: '', fields: [], sections: [], ignore_bands: [],
  expand_values: false, look_every_page: true, continue_across_pages: false, pending: null,
};

type Layout = SavedExtractionTemplate;
type LayoutChanges = Partial<Pick<Layout, 'draft' | 'repeat_group_id' | 'reference_row_id'>>;
const draftKey = (layout: Layout) => JSON.stringify({ draft: layout.draft, repeat_group_id: layout.repeat_group_id, reference_row_id: layout.reference_row_id });
const message = (cause: unknown) => cause instanceof Error ? cause.message : String(cause);
const pendingWrites = new Map<string, Promise<unknown>>();
const unsavedDrafts = new Map<string, Map<number, Layout>>();

/** Serialize saves so a slow response cannot overwrite a newer draft. */
export function useExtractionLayouts(projectId: string, sheetId: string, source: string, refreshKey?: unknown) {
  const context = `${projectId}:${sheetId}:${source}`;
  const [layouts, setLayouts] = useState<Layout[]>([]);
  const [layout, setLayout] = useState<Layout | null>(null);
  const [loading, setLoading] = useState(true);
  const [switching, setSwitching] = useState(false);
  const [saving, setSaving] = useState(false);
  const [acknowledged, setAcknowledged] = useState<Record<number, string>>({});
  const [error, setError] = useState<string | null>(null);
  const current = useRef<Layout | null>(null);
  const activeContext = useRef(context);
  const mounted = useRef(false);
  const savedKeys = useRef(new Map<number, string>());
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const inFlight = useRef(new Map<Promise<Layout>, string>());
  const latestSave = useRef<{ key: string; work: Promise<Layout> } | null>(null);

  const activate = useCallback((next: Layout) => {
    current.current = next;
    setLayout(next);
    setError(null);
  }, []);

  const save = useCallback((snapshot: Layout): Promise<Layout> => {
    const key = draftKey(snapshot);
    const requestKey = `${context}:${snapshot.id}:${key}`;
    const last = latestSave.current;
    // Only coalesce the final queued write: A → B → A must finish on A.
    if (last?.key === requestKey && pendingWrites.get(context) === last.work) return last.work;
    if (savedKeys.current.get(snapshot.id) === key && !pendingWrites.has(context)) {
      unsavedDrafts.get(context)?.delete(snapshot.id);
      return Promise.resolve(snapshot);
    }
    const active = () => mounted.current && activeContext.current === context;
    if (active()) { setSaving(true); setError(null); }
    const work = (pendingWrites.get(context) ?? queue.current).catch(() => undefined).then(() => documentExtractionApi.save(projectId, {
      id: snapshot.id, sheet_id: Number(sheetId), source, draft: snapshot.draft,
      repeat_group_id: snapshot.repeat_group_id, reference_row_id: snapshot.reference_row_id,
    })).then((saved) => {
      savedKeys.current.set(snapshot.id, key);
      const cached = unsavedDrafts.get(context);
      const unsaved = cached?.get(snapshot.id);
      if (unsaved && draftKey(unsaved) === key) cached?.delete(snapshot.id);
      if (active()) {
        setLayouts((items) => items.map((item) => item.id === saved.id ? saved : item));
        setAcknowledged((previous) => ({ ...previous, [snapshot.id]: key }));
        if (current.current?.id === snapshot.id && draftKey(current.current) === key) setError(null);
      }
      return saved;
    }).catch((cause: unknown) => {
      if (active() && current.current?.id === snapshot.id && draftKey(current.current) === key) setError(message(cause));
      throw cause;
    }).finally(() => {
      inFlight.current.delete(work);
      if (active()) setSaving([...inFlight.current.values()].includes(context));
    });
    inFlight.current.set(work, context);
    latestSave.current = { key: requestKey, work };
    queue.current = work;
    pendingWrites.set(context, work);
    void work.finally(() => { if (pendingWrites.get(context) === work) pendingWrites.delete(context); }).catch(() => undefined);
    return work;
  }, [context, projectId, sheetId, source]);

  useEffect(() => {
    const controller = new AbortController();
    activeContext.current = context;
    mounted.current = true;
    current.current = null;
    if (!source) return () => { controller.abort(); mounted.current = false; };
    void (pendingWrites.get(context) ?? Promise.resolve()).catch(() => undefined).then(() => {
      if (controller.signal.aborted) return null;
      setLayout(null); setLayouts([]); setLoading(true); setError(null);
      return documentExtractionApi.templates(projectId, sheetId, source, controller.signal);
    }).then(async (response) => {
      if (!response) return;
      if (controller.signal.aborted) return;
      const cached = unsavedDrafts.get(context);
      const items = response.templates.map((item) => {
        savedKeys.current.set(item.id, draftKey(item));
        const draft = cached?.get(item.id);
        return draft ? { ...item, draft: draft.draft, repeat_group_id: draft.repeat_group_id, reference_row_id: draft.reference_row_id } : item;
      });
      let selected = items.find((item) => item.id === response.selected_layout_id) ?? items[0];
      if (!selected) {
        selected = await documentExtractionApi.save(projectId, { sheet_id: Number(sheetId), source, draft: EMPTY_EXTRACTION_DRAFT });
        items.push(selected);
      }
      if (controller.signal.aborted) return;
      if (!savedKeys.current.has(selected.id)) savedKeys.current.set(selected.id, draftKey(selected));
      setAcknowledged(Object.fromEntries(savedKeys.current));
      setLayouts(items); activate(selected);
      if (response.selected_layout_id !== selected.id) await documentExtractionApi.select(projectId, { sheet_id: Number(sheetId), source, layout_id: selected.id });
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(message(cause)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => {
      controller.abort();
      mounted.current = false;
      // Flush the captured source context when leaving Extract or changing sources.
      const snapshot = current.current;
      if (snapshot) void save(snapshot).catch(() => undefined);
    };
  }, [context, projectId, sheetId, source, activate, save]);

  const change = useCallback((changes: LayoutChanges | ((previous: Layout) => LayoutChanges)) => {
    const previous = current.current;
    if (!previous) return;
    const next = { ...previous, ...(typeof changes === 'function' ? changes(previous) : changes) };
    const cached = unsavedDrafts.get(context) ?? new Map<number, Layout>();
    cached.set(next.id, next);
    unsavedDrafts.set(context, cached);
    current.current = next;
    setLayout(next);
  }, [context]);

  useEffect(() => {
    if (!current.current || !source) return;
    const controller = new AbortController();
    void documentExtractionApi.templates(projectId, sheetId, source, controller.signal).then((response) => {
      if (controller.signal.aborted) return;
      const applied = new Map(response.templates.map((item) => [item.id, item.has_applied]));
      setLayouts((items) => items.map((item) => ({ ...item, has_applied: applied.get(item.id) ?? item.has_applied })));
      const previous = current.current;
      if (previous && applied.has(previous.id)) {
        const next = { ...previous, has_applied: applied.get(previous.id)! };
        current.current = next;
        setLayout(next);
      }
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(message(cause)); });
    return () => controller.abort();
  }, [context, projectId, sheetId, source, refreshKey]);

  useEffect(() => {
    if (!layout || savedKeys.current.get(layout.id) === draftKey(layout)) return;
    const timer = window.setTimeout(() => { void save(layout).catch(() => undefined); }, 450);
    return () => window.clearTimeout(timer);
  }, [layout, acknowledged, save]);

  const flush = async () => {
    const snapshot = current.current;
    if (!snapshot) throw new Error('Choose a layout first.');
    return save(snapshot);
  };

  const choose = async (id: number | null) => {
    setSwitching(true); setError(null);
    try {
      await flush();
      const next = id === null
        ? await documentExtractionApi.save(projectId, { sheet_id: Number(sheetId), source, draft: EMPTY_EXTRACTION_DRAFT })
        : layouts.find((item) => item.id === id);
      if (!next) throw new Error('This layout is unavailable. Reload Extract and try again.');
      await documentExtractionApi.select(projectId, { sheet_id: Number(sheetId), source, layout_id: next.id });
      if (!mounted.current || activeContext.current !== context) return;
      savedKeys.current.set(next.id, draftKey(next));
      setAcknowledged((previous) => ({ ...previous, [next.id]: draftKey(next) }));
      setLayouts((items) => id === null ? [...items, next] : items);
      activate(next);
    } catch (cause) { if (mounted.current) setError(message(cause)); }
    finally { if (mounted.current) setSwitching(false); }
  };

  const dirty = Boolean(layout && acknowledged[layout.id] !== draftKey(layout));
  useEffect(() => {
    if (!dirty && !saving) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty, saving]);

  return { layouts, layout, loading, switching, saving, error, change, choose, flush, dirty };
}
