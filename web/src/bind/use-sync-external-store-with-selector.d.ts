// `use-sync-external-store` ships no bundled types (verified: no `types`
// field, no .d.ts in the published package as of 1.6.0). This is the
// upstream shape (matches DefinitelyTyped's @types/use-sync-external-store,
// which this repo does not depend on — vendor the runtime shim only, not a
// second package for its types, when a ~10-line ambient declaration suffices).
declare module 'use-sync-external-store/with-selector' {
  export function useSyncExternalStoreWithSelector<Snapshot, Selection>(
    subscribe: (onStoreChange: () => void) => () => void,
    getSnapshot: () => Snapshot,
    getServerSnapshot: undefined | null | (() => Snapshot),
    selector: (snapshot: Snapshot) => Selection,
    isEqual?: (a: Selection, b: Selection) => boolean,
  ): Selection;
}
