import { useEffect, useMemo } from 'react';
import type { ActionCatalogPayload, EngineOption } from '../api/open';
import { useActionCatalogHandle } from '../bind/useActionCatalogHandle';
import { useSelector } from '../bind/useSelector';

/** Comparisons project the accepted workspace catalog; they never fetch a copy. */
export function useComparisonCatalog(
  enginesFromCatalog: (catalog: ActionCatalogPayload | null | undefined) => EngineOption[],
  fallback: EngineOption[],
) {
  const owner = useActionCatalogHandle();
  const catalog = useSelector(owner.store, (state) => state.catalog);
  const status = useSelector(owner.store, (state) => state.status);
  const error = useSelector(owner.store, (state) => state.error);
  useEffect(() => {
    // Supports standalone comparison hosts; normal workspace activation already starts it.
    if (owner.store.get().status === 'idle') void owner.start();
  }, [owner]);
  const engines = useMemo(() => catalog ? enginesFromCatalog(catalog) : fallback,
    [catalog, enginesFromCatalog, fallback]);
  return { engines, status, error, retry: owner.refresh };
}
