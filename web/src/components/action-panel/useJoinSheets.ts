import { useEffect, useRef, useState } from 'react';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import type { SheetMeta } from '../../api/open';

export function useJoinSheets(primary: SheetMeta) {
  const { projectApi } = useWorkspaceStores();
  const [revision, refresh] = useState(0);
  const [loaded, setLoaded] = useState<{ sheets: SheetMeta[]; error: string | null;
    revision: number; primaryRevision: string } | null>(null);
  const primaryRevision = JSON.stringify([primary.id, primary.name,
    primary.columns.map(({ id, name, type }) => [id, name, type])]);
  useEffect(() => {
    let current = true;
    projectApi.listSheets().then((sheets) => {
      if (current) setLoaded({ sheets, error: null, revision, primaryRevision });
    }).catch((error: unknown) => {
      if (current) setLoaded((before) => ({ sheets: before?.sheets ?? [],
        error: error instanceof Error ? error.message : 'Could not load sheets.',
        revision, primaryRevision }));
    });
    return () => { current = false; };
  }, [projectApi, revision, primaryRevision]);
  const loading = !loaded || loaded.revision !== revision || loaded.primaryRevision !== primaryRevision;
  return { sheets: loaded?.sheets ?? [], error: loading ? null : loaded?.error ?? null,
    loading,
    refresh: () => refresh((value) => value + 1) };
}

export interface JoinColumnReference { slot: string; sheetId: number; name: string }
interface Pin { sheetId: number; columnId: string | null; emitted: string; label: string }

/** Session-local editor identity, not saved authority. A removed source clears
 * its public name until explicitly repaired, even if another column reuses it. */
export function useJoinColumnPins(sheets: SheetMeta[], ready: boolean,
  references: JoinColumnReference[], replaceNames: (names: ReadonlyMap<string, string>) => void) {
  const pins = useRef(new Map<string, Pin>());
  const [missing, setMissing] = useState<string[]>([]);
  useEffect(() => {
    if (!ready) return;
    const changes = new Map<string, string>();
    const absent: string[] = [];
    const slots = new Set(references.map(({ slot }) => slot));
    for (const slot of pins.current.keys()) if (!slots.has(slot)) pins.current.delete(slot);
    for (const ref of references) {
      const sheet = sheets.find((item) => Number(item.id) === ref.sheetId);
      const previous = pins.current.get(ref.slot);
      let pin = previous;
      if (!pin || pin.sheetId !== ref.sheetId || pin.emitted !== ref.name) {
        const column = sheet?.columns.find((item) => item.name === ref.name);
        pin = { sheetId: ref.sheetId, columnId: column ? String(column.id) : null,
          emitted: ref.name, label: ref.name };
        pins.current.set(ref.slot, pin);
      }
      const current = sheet?.columns.find((item) => String(item.id) === pin.columnId);
      const name = current?.name ?? '';
      if (pin.emitted !== name) {
        changes.set(ref.slot, name);
        pin.emitted = name;
      }
      if (current) pin.label = current.name;
      else if (pin.label) absent.push(pin.label);
    }
    // eslint-disable-next-line react-hooks/set-state-in-effect -- pin reconciliation preserves rename/deletion identity outside Params.
    setMissing((before) => JSON.stringify(before) === JSON.stringify(absent) ? before : absent);
    if (changes.size) replaceNames(changes);
  }, [sheets, ready, references, replaceNames]);
  return missing;
}
