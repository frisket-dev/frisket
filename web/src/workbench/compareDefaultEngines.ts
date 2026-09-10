import type { EngineOption } from '../api/open';

// A compare tab's `defaultEngineIds` are seeded before the action catalog has
// answered — they are a GUESS about what this install can run, and on any
// install without the sidecar configured the guess is wrong (OCR Compare
// shipped `['rapidocr', 'dots.mocr']`; `dots.mocr` is a sidecar engine, so the second
// column of the very first compare a new user opens could only ever fail).
// Once the catalog lands, the session re-points still-untouched default
// variants at engines the install actually has.

/** Resolve seeded default engine ids against the catalog the install reports.
 *
 *  A preferred id the catalog marks unavailable is replaced by the first
 *  available engine no other default has claimed — ids the preferred list can
 *  still take are reserved first, so a substitution never duplicates a column
 *  that a later preferred entry will fill. With no substitute left, the
 *  unavailable default is DROPPED rather than kept: a base install where only
 *  RapidOCR is available should open on one runnable variant in Survey (the
 *  shape TopicSegmentationCompareTab already seeds), not on a second column
 *  whose only possible outcome is a configuration error.
 *
 *  The exception is an install with NOTHING available, which keeps the first
 *  preferred id: one chip naming a real engine, whose Configure popover
 *  carries the catalog's own reason, beats an empty tab that never says why.
 *
 *  An empty catalog (nothing loaded yet) returns `preferred` unchanged. */
export function resolveDefaultEngineIds(
  preferred: readonly string[],
  catalog: readonly EngineOption[],
): string[] {
  if (catalog.length === 0 || preferred.length === 0) return [...preferred];
  const byId = new Map(catalog.map((engine) => [engine.id, engine]));
  const isAvailable = (id: string) => {
    const engine = byId.get(id);
    return engine !== undefined && engine.available !== false;
  };
  // Ids a preferred entry can still take are reserved, so a substitution never
  // duplicates a column a later preferred entry will fill.
  const reserved = new Set(preferred.filter(isAvailable));
  const substitutes = catalog.flatMap((engine) =>
    engine.available !== false && !reserved.has(engine.id) ? [engine.id] : [],
  );
  let cursor = 0;
  const resolved = preferred.flatMap((id) => {
    if (isAvailable(id)) return [id];
    const substitute = substitutes[cursor];
    if (substitute === undefined) return [];
    cursor += 1;
    return [substitute];
  });
  return resolved.length > 0 ? resolved : [preferred[0]];
}

/** True when `columns` already holds exactly `preferred` — same length, same
 *  engine ids, same order.
 *
 *  Two callers, both in the session's catalog-time re-point: it is how "the
 *  user has not touched the seeded defaults" is decided (any add/remove/
 *  engine-change makes it false, so a real choice is never overwritten), and
 *  how "the resolved list is what's already there" short-circuits the update. */
export function isUntouchedDefaultSeed(
  columns: ReadonlyArray<{ engineId: string | null }>,
  preferred: readonly string[],
): boolean {
  return (
    columns.length === preferred.length &&
    columns.every((column, index) => column.engineId === preferred[index])
  );
}
