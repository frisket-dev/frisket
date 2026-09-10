// Shared column plumbing for the resolve drawers (Substitute / Replace /
// Combine): the text-like column roster + preselect, the enumeration fetch
// with its monotonic request-id guard, and the reload-without-reset recovery
// path with its drop notice. Column-scoped authoring state does NOT live
// here — the forms key their body component on `${sheetId}:${column}` so a
// column switch remounts (and thereby resets) it instead of hand-clearing
// every useState.
import { useEffect, useMemo, useRef, useState } from 'react';
import { type ColumnValuesPreview, type SheetMeta } from '../../api/open';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';

/** Column types the resolve actions operate on. */
const TEXT_LIKE = new Set(['text', 'category', 'link']);

const msg = (e: unknown): string => (e instanceof Error ? e.message : String(e));

/** Text-like column roster + selection. `initialColumn` preselects when it
 *  names a text-like column (e.g. launched from a column ▾ menu), otherwise
 *  the first one; `activeColumn` re-derives when the sheet's columns change
 *  under the selection. */
export function useResolveColumns(sheet: SheetMeta, initialColumn?: string) {
  const columns = useMemo(
    () => sheet.columns.filter((c) => TEXT_LIKE.has(c.type)),
    [sheet.columns],
  );
  const [columnName, setColumnName] = useState(
    initialColumn && columns.some((c) => c.name === initialColumn)
      ? initialColumn
      : columns[0]?.name ?? '',
  );
  const activeColumn = columns.some((c) => c.name === columnName)
    ? columnName
    : columns[0]?.name ?? '';
  return { columns, activeColumn, setColumnName };
}

/** Output-column naming: a free-text draft whose blank state falls back to
 *  the `{input_column}_clean` default the executors also assume. */
export interface UseColumnValuesPreviewOptions {
  sheetId: string;
  /** '' = the sheet has no text-like column; nothing is fetched. */
  column: string;
  /** Page size; omitted = the endpoint's default (and the request carries no
   *  `limit` key at all — the forms' tests pin the exact payload). */
  limit?: number;
  /** Noun for the reload drop notice ("mapped value" / "grouped value"). */
  droppedNoun: string;
  /** Fired for every landed enumeration, initial fetch and reload alike
   *  (Substitute flips to paste mode above its enumeration threshold). */
  onPreview?(out: ColumnValuesPreview, source: 'load' | 'reload'): void;
  /** Reload-without-reset: re-key authored state against the fresh
   *  enumeration and return how many authored entries were dropped — the
   *  count drives the notice. */
  pruneOnReload?(out: ColumnValuesPreview): number;
}

/** The enumeration (read-only column-values preview) behind Substitute and
 *  Combine. A monotonic request id guards every response: one that lands
 *  after a newer request (a reload; column switches unmount the caller) is
 *  discarded, never merged into fresher state. `reload` is the stale-replay
 *  recovery path — it keeps authored state (via `pruneOnReload`) so the
 *  fresh input snapshot can ride the next Apply without redoing the authoring. */
export function useColumnValuesPreview(opts: UseColumnValuesPreviewOptions) {
  const { projectApi: api } = useWorkspaceStores();
  const { sheetId, column, limit, droppedNoun } = opts;
  const [preview, setPreview] = useState<ColumnValuesPreview | null>(null);
  // True from the first paint whenever a column exists: consumers key their
  // body component on `${sheetId}:${column}`, so this hook mounts fresh per
  // column and the mount effect below always starts a fetch — no synchronous
  // setState in the effect body needed (react-hooks/set-state-in-effect).
  const [loading, setLoading] = useState(Boolean(column));
  const [error, setError] = useState<string | null>(null);
  /** Post-reload note ("N … values no longer in the column were dropped"). */
  const [reloadNotice, setReloadNotice] = useState<string | null>(null);
  const genRef = useRef(0);
  // Latest-callback ref so the fetch effect never re-arms on a render-fresh
  // closure — consumers pass inline callbacks over their authoring state.
  const optsRef = useRef(opts);
  useEffect(() => {
    optsRef.current = opts;
  });

  useEffect(() => {
    if (!column) return;
    const gen = (genRef.current += 1);
    api
      .columnValuesPreview(
        limit === undefined
          ? { sheetId, inputColumn: column }
          : { sheetId, inputColumn: column, limit },
      )
      .then((out) => {
        if (gen !== genRef.current) return;
        setPreview(out);
        setError(null);
        optsRef.current.onPreview?.(out, 'load');
      })
      .catch((e: unknown) => {
        if (gen !== genRef.current) return;
        setError(msg(e));
      })
      .finally(() => {
        if (gen === genRef.current) setLoading(false);
      });
  }, [api, sheetId, column, limit]);

  const reload = () => {
    if (!column) return;
    const gen = (genRef.current += 1);
    setError(null);
    setLoading(true);
    api
      .columnValuesPreview(
        limit === undefined
          ? { sheetId, inputColumn: column }
          : { sheetId, inputColumn: column, limit },
      )
      .then((out) => {
        if (gen !== genRef.current) return;
        setPreview(out);
        opts.onPreview?.(out, 'reload');
        const dropped = opts.pruneOnReload?.(out) ?? 0;
        setReloadNotice(
          dropped > 0
            ? `Values reloaded — ${dropped.toLocaleString()} ${droppedNoun}${dropped === 1 ? '' : 's'} no longer in the column ${dropped === 1 ? 'was' : 'were'} dropped.`
            : null,
        );
      })
      .catch((e: unknown) => {
        if (gen !== genRef.current) return;
        setError(msg(e));
      })
      .finally(() => {
        if (gen === genRef.current) setLoading(false);
      });
  };

  return { preview, loading, error, reloadNotice, reload };
}
