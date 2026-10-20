import { createElement, memo, useCallback, useEffect, useMemo, useState } from 'react';
import type { HttpSelectorChoicesQuery } from '../api/selectorChoices';
import { currentSelectorChoice } from '../engine-selector/selectorProjection';
import { useSelectorChoices, type SelectorChoicesState } from '../engine-selector/useSelectorChoices';
import { mediaSelectorQuery, type MediaSelectorAction } from './mediaCompareSelector';
import type { CompareColumn } from './mediaCompareSession';

const WAITING: SelectorChoicesState = {
  response: null, stale: true, loading: true, refreshing: false,
  error: null, operationError: null, refresh: async () => false,
};
type Projection = { key: string; scope: string; state: SelectorChoicesState };
const fullQueryKey = (query: HttpSelectorChoicesQuery) => JSON.stringify(query);

/** One lifecycle per variant, whether or not its configure picker is open. */
const VariantSelectorOwner = memo(function VariantSelectorOwner({ projectId, query, id, scopeKey, scope, publish }: {
  projectId: string | null | undefined;
  query: HttpSelectorChoicesQuery;
  id: string;
  scopeKey: string;
  scope: string;
  publish(id: string, projection: Projection): void;
}) {
  const state = useSelectorChoices({ projectId, query, queryKey: fullQueryKey });
  const { response, stale, loading, refreshing, error, operationError, refresh } = state;
  useEffect(() => {
    publish(id, { key: scopeKey, scope, state: { response, stale, loading, refreshing, error, operationError, refresh } });
  }, [id, scopeKey, scope, publish, response, stale, loading, refreshing, error, operationError, refresh]);
  return null;
});

/** Picker and Run read the exact same accepted projection, never mirrored booleans. */
export function useCompareSelectorReadiness(
  projectId: string | null | undefined, actionId: MediaSelectorAction, columns: CompareColumn[],
) {
  const [projections, setProjections] = useState<Record<string, Projection>>({});
  const scope = JSON.stringify([projectId, actionId]);
  const keyFor = useCallback((column: CompareColumn) =>
    JSON.stringify([projectId, mediaSelectorQuery(actionId, column)]), [actionId, projectId]);
  const publish = useCallback((id: string, projection: Projection) => {
    setProjections((previous) => ({ ...previous, [id]: projection }));
  }, []);
  const selectorOwners = useMemo(() => columns.map((column) => createElement(VariantSelectorOwner, {
    key: `${projectId}:${actionId}:${column.id}`, projectId, id: column.id,
    query: mediaSelectorQuery(actionId, column), scopeKey: keyFor(column), scope, publish,
  })), [columns, projectId, actionId, scope, keyFor, publish]);
  const selectorProjection = useCallback((column: CompareColumn): SelectorChoicesState => {
    const previous = projections[column.id];
    if (!previous || previous.scope !== scope) return WAITING;
    if (previous.key === keyFor(column)) return previous.state;
    // Preserve an open setup dialog during the render before its owner sees the
    // new query, but never admit that old projection as current readiness.
    return { ...previous.state, stale: true, refresh: WAITING.refresh };
  }, [projections, scope, keyFor]);
  const isRunnable = useCallback((column: CompareColumn) => {
    const choice = currentSelectorChoice(selectorProjection(column));
    const authored = choice?.authored_selection;
    return Boolean(choice?.can_run && (authored?.kind === 'engine' || authored?.kind === 'engine_model')
      && authored.engine === column.engineId);
  }, [selectorProjection]);
  return { isRunnable, selectorProjection, selectorOwners };
}
