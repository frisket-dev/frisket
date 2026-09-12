import { useCallback, useEffect, useRef, useState } from 'react';
import { selectorChoicesApi, type SelectorChoice } from '../api/selectorChoices';
import { mediaSelectorQuery, type MediaSelectorAction } from './mediaCompareSelector';
import type { CompareColumn } from './mediaCompareSession';

/** Short-lived project/action projections for the comparison's current variants. */
export function useCompareSelectorReadiness(
  projectId: string | null | undefined, actionId: MediaSelectorAction, columns: CompareColumn[],
) {
  const [readiness, setReadiness] = useState<Record<string, { key: string; canRun: boolean }>>({});
  const readinessVersions = useRef(new Map<string, number>());
  const currentColumns = useRef(columns);
  currentColumns.current = columns;
  const readinessKey = useCallback((column: CompareColumn) =>
    JSON.stringify([projectId, mediaSelectorQuery(actionId, column)]),
  [actionId, projectId]);
  const reportColumnChoice = useCallback((column: CompareColumn, choice: SelectorChoice | null) => {
    const current = currentColumns.current.find((item) => item.id === column.id);
    if (!current || readinessKey(current) !== readinessKey(column)) return;
    // A fresh selector projection supersedes any earlier background read.
    readinessVersions.current.set(column.id, (readinessVersions.current.get(column.id) ?? 0) + 1);
    const key = readinessKey(column);
    const selection = choice?.authored_selection;
    const matches = selection?.kind === 'engine' || selection?.kind === 'engine_model'
      ? selection.engine === column.engineId : false;
    const canRun = matches && Boolean(choice?.can_run);
    setReadiness((previous) => previous[column.id]?.key === key && previous[column.id].canRun === canRun
      ? previous : { ...previous, [column.id]: { key, canRun } });
  }, [readinessKey]);

  useEffect(() => {
    if (!projectId) return undefined;
    const controller = new AbortController();
    for (const column of columns) {
      if (!column.engineId) continue;
      const version = (readinessVersions.current.get(column.id) ?? 0) + 1;
      readinessVersions.current.set(column.id, version);
      const key = readinessKey(column);
      void selectorChoicesApi.getSelectorChoices(projectId,
        mediaSelectorQuery(actionId, column), { signal: controller.signal })
        .then((response) => {
          if (controller.signal.aborted || readinessVersions.current.get(column.id) !== version) return;
          const choice = response.groups.flatMap((group) => group.choices)
            .find((item) => item.choice_id === response.current_choice_id);
          setReadiness((previous) => ({ ...previous, [column.id]: { key, canRun: Boolean(choice?.can_run) } }));
        })
        .catch(() => {
          if (controller.signal.aborted || readinessVersions.current.get(column.id) !== version) return;
          setReadiness((previous) => ({ ...previous, [column.id]: { key, canRun: false } }));
        });
    }
    return () => controller.abort();
  }, [columns, actionId, projectId, readinessKey]);
  const isRunnable = useCallback((column: CompareColumn) =>
    readiness[column.id]?.key === readinessKey(column) && readiness[column.id].canRun,
  [readiness, readinessKey]);
  return { isRunnable, reportColumnChoice };
}
