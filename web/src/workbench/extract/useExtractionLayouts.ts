import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { documentExtractionApi, type SavedExtractionTemplate } from '../../api/documentExtraction';
import {
  extractionLayoutDraftKey,
  type ExtractionLayoutContext,
  type ExtractionLayoutStoreHandle,
} from '../../state/extractionLayoutStore';

export const EMPTY_EXTRACTION_DRAFT = {
  reference_blob_id: '', reference_page: null, reference_fingerprint: '', fields: [], sections: [], ignore_bands: [],
  expand_values: false, look_every_page: true, continue_across_pages: false, pending: null,
};

type Layout = SavedExtractionTemplate;
type LayoutChanges = Partial<Pick<Layout, 'draft' | 'repeat_group_id' | 'reference_row_id'>>;
const message = (cause: unknown) => cause instanceof Error ? cause.message : String(cause);

/** Serialize saves so a slow response cannot overwrite a newer draft. */
export function useExtractionLayouts(store: ExtractionLayoutStoreHandle, sheetId: string, source: string, refreshKey?: unknown) {
  const projectId = store.projectId;
  const context = useMemo(() => store.getContext(sheetId, source), [sheetId, source, store]);
  const [layouts, setLayouts] = useState<Layout[]>([]);
  const [layout, setLayout] = useState<Layout | null>(null);
  const [loading, setLoading] = useState(true);
  const [switching, setSwitching] = useState(false);
  const [saving, setSaving] = useState(false);
  const [acknowledged, setAcknowledged] = useState<Record<number, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const current = useRef<Layout | null>(null);
  const activeContext = useRef<ExtractionLayoutContext>(context);
  const mounted = useRef(false);
  const inFlight = useRef(new Map<Promise<Layout>, ExtractionLayoutContext>());

  const activate = useCallback((next: Layout) => {
    current.current = next;
    setLayout(next);
    setError(null);
    setSaveError(null);
  }, []);

  const save = useCallback((snapshot: Layout): Promise<Layout> => {
    const key = extractionLayoutDraftKey(snapshot);
    const active = () => mounted.current && activeContext.current === context;
    if (active()) { setSaving(true); setSaveError(null); }
    const work = context.save(snapshot, () => documentExtractionApi.save(projectId, {
      id: snapshot.id, sheet_id: Number(sheetId), source, draft: snapshot.draft,
      repeat_group_id: snapshot.repeat_group_id, reference_row_id: snapshot.reference_row_id,
    })).then((saved) => {
      if (active()) {
        setLayouts((items) => items.map((item) => item.id === saved.id ? saved : item));
        setAcknowledged((previous) => ({ ...previous, [snapshot.id]: key }));
        if (current.current?.id === snapshot.id && extractionLayoutDraftKey(current.current) === key) setSaveError(null);
      }
      return saved;
    }).catch((cause: unknown) => {
      if (active() && current.current?.id === snapshot.id && extractionLayoutDraftKey(current.current) === key) setSaveError(message(cause));
      throw cause;
    }).finally(() => {
      inFlight.current.delete(work);
      if (active()) setSaving([...inFlight.current.values()].includes(context));
    });
    inFlight.current.set(work, context);
    return work;
  }, [context, projectId, sheetId, source]);

  useEffect(() => {
    const controller = new AbortController();
    activeContext.current = context;
    mounted.current = true;
    context.retain();
    current.current = null;
    if (!source) return () => { controller.abort(); mounted.current = false; context.release(); };
    void context.waitForPending().catch(() => undefined).then(() => {
      if (controller.signal.aborted) return null;
      setLayout(null); setLayouts([]); setLoading(true); setSwitching(false); setError(null); setSaveError(null);
      return documentExtractionApi.templates(projectId, sheetId, source, controller.signal);
    }).then(async (response) => {
      if (!response) return;
      if (controller.signal.aborted) return;
      const items = response.templates.map((item) => context.restore(item));
      let selected = items.find((item) => item.id === response.selected_layout_id) ?? items[0];
      if (!selected) {
        selected = await documentExtractionApi.save(projectId, { sheet_id: Number(sheetId), source, draft: EMPTY_EXTRACTION_DRAFT });
        items.push(selected);
      }
      if (controller.signal.aborted) return;
      if (context.savedKey(selected.id) === undefined) context.rememberSaved(selected);
      setAcknowledged(context.acknowledged());
      setLayouts(items); activate(selected);
      if (response.selected_layout_id !== selected.id) await documentExtractionApi.select(projectId, { sheet_id: Number(sheetId), source, layout_id: selected.id });
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(`Could not load layouts: ${message(cause)}`); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => {
      controller.abort();
      mounted.current = false;
      // Flush the captured source context when leaving Extract or changing sources.
      const snapshot = current.current;
      if (snapshot) void save(snapshot).catch(() => undefined);
      context.release();
    };
  }, [context, projectId, sheetId, source, activate, save, loadAttempt]);

  const change = useCallback((changes: LayoutChanges | ((previous: Layout) => LayoutChanges)) => {
    const previous = current.current;
    if (!previous) return;
    const next = { ...previous, ...(typeof changes === 'function' ? changes(previous) : changes) };
    context.cacheDraft(next);
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
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(`Could not refresh layout documents: ${message(cause)}`); });
    return () => controller.abort();
  }, [context, projectId, sheetId, source, refreshKey]);

  useEffect(() => {
    if (!layout || context.savedKey(layout.id) === extractionLayoutDraftKey(layout)) return;
    const timer = window.setTimeout(() => { void save(layout).catch(() => undefined); }, 450);
    return () => window.clearTimeout(timer);
  }, [layout, acknowledged, context, save]);

  const flush = async () => {
    const snapshot = current.current;
    if (!snapshot) throw new Error('Choose a layout first.');
    return save(snapshot);
  };

  const choose = async (id: number | null) => {
    const active = () => mounted.current && activeContext.current === context;
    setSwitching(true); setError(null);
    try { await flush(); }
    catch { if (active()) setSwitching(false); return; }
    try {
      const next = id === null
        ? await documentExtractionApi.save(projectId, { sheet_id: Number(sheetId), source, draft: EMPTY_EXTRACTION_DRAFT })
        : layouts.find((item) => item.id === id);
      if (!next) throw new Error('This layout is unavailable. Reload Extract and try again.');
      await documentExtractionApi.select(projectId, { sheet_id: Number(sheetId), source, layout_id: next.id });
      if (!active()) return;
      if (id === null) {
        context.rememberSaved(next);
        setAcknowledged((previous) => ({ ...previous, [next.id]: extractionLayoutDraftKey(next) }));
      }
      setLayouts((items) => id === null ? [...items, next] : items);
      activate(next);
    } catch (cause) { if (active()) setError(`Could not ${id === null ? 'create' : 'select'} layout: ${message(cause)}`); }
    finally { if (active()) setSwitching(false); }
  };

  const dirty = Boolean(layout && acknowledged[layout.id] !== extractionLayoutDraftKey(layout));
  useEffect(() => {
    if (!dirty && !saving) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty, saving]);

  const retryLoad = () => setLoadAttempt((attempt) => attempt + 1);
  return { layouts, layout, loading, switching, saving, error, saveError, change, choose, flush, retryLoad, dirty };
}
