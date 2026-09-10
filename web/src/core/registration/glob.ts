// Generic collision-safe collector for import.meta.glob's EAGER module maps.
// Every glob-registered first-party inventory (commands:
// core/commands/first-party/*.command.ts; work views:
// core/selectors/views/*.view.ts) is a directory of one-file-per-entry modules,
// each default-exporting its descriptor. This is the ONE place that walks an
// eager glob result, pulls each module's default export, and asserts no two
// files claim the same registration key.
//
// A hand-maintained object-literal inventory drifts: a new entry requires
// editing a shared list, and a duplicate/misspelled key silently overwrites a
// prior entry with no error. Glob self-registration makes "new capability = new
// file"; this collector makes a colliding key a load-time THROW instead of a
// silent last-write-wins overwrite.
//
// Framework-free (core/ boundary): no react/react-dom, no state/bind/app-layer
// imports.

export interface EagerModules<T> {
  readonly [path: string]: { readonly default: T };
}

/**
 * Collects `{ path -> { default } }` (import.meta.glob's `{ eager: true }`
 * shape) into a `{ key -> value }` map, throwing at call time if two
 * different files produce the same key. Callers invoke this at module scope
 * in their own glob-barrel file (e.g. core/commands/registry.ts), so a
 * collision fails as soon as that module is first imported — in the app at
 * startup, in tests on first import of the registry.
 */
export function collectEagerRegistrations<T>(
  modules: EagerModules<T>,
  keyOf: (value: T) => string,
  kindLabel: string,
): Record<string, T> {
  const byKey: Record<string, T> = {};
  const pathByKey: Record<string, string> = {};
  for (const [path, mod] of Object.entries(modules)) {
    const value = mod.default;
    const key = keyOf(value);
    const existingPath = pathByKey[key];
    if (existingPath !== undefined) {
      throw new Error(
        `duplicate ${kindLabel} registration for '${key}': ${existingPath} and ${path} both claim it`,
      );
    }
    byKey[key] = value;
    pathByKey[key] = path;
  }
  return byKey;
}
