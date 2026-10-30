import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { EvaluationApi, EvaluationLink, EvaluationRequest, EvaluationResponse, SavedSummary } from '../../api/evaluation';
import type { ActionCompareSessionSnapshot } from '../actionCompareSession';
import { comparisonRequest } from './comparisonEvaluation';

function matchingShelf(items: SavedSummary[], columns: ActionCompareSessionSnapshot['context']['output_columns']) {
  const signature = (outputs: typeof columns | SavedSummary['output_columns']) => outputs
    .map((column) => `${column.logical_key}:${column.column_id ?? `name:${column.current_name}`}`).sort().join('\0');
  const outputs = signature(columns);
  return items.filter((item) => signature(item.output_columns) === outputs);
}

export function useComparisonEvaluation(api: EvaluationApi | undefined, state: ActionCompareSessionSnapshot, dataRevision = 0) {
  const [links, setLinks] = useState<EvaluationLink[]>([]);
  const [items, setItems] = useState<SavedSummary[]>([]);
  const [scoreState, setScoreState] = useState<{ key: string; requestKey: string; response: EvaluationResponse | null }>({ key: '', requestKey: '', response: null });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);
  const sequence = useRef(0);
  const actionId = state.context.draft.action_id;
  const sheetId = state.context.sheet.id;
  const outputColumns = state.context.output_columns;
  const outputIds = useMemo(() => new Set(outputColumns.flatMap((column) => column.column_id ? [column.column_id] : [])), [outputColumns]);
  const link = links.find((candidate) => candidate.action_id === actionId
    && candidate.fields.some((field) => outputIds.has(field.output_column_id))) ?? null;
  const requestKey = JSON.stringify(comparisonRequest(state, link?.id ?? null));
  const request = useMemo<EvaluationRequest>(() => JSON.parse(requestKey), [requestKey]);
  const scoreKey = `${dataRevision}:${generation}:${requestKey}`;
  const loading = Boolean(api && request.variants.length && scoreState.key !== scoreKey);
  const response = scoreState.requestKey === requestKey ? scoreState.response : null;
  const setResponse = useCallback((value: EvaluationResponse) => setScoreState({ key: scoreKey, requestKey, response: value }), [scoreKey, requestKey]);
  const setLink = useCallback((value: EvaluationLink) => setLinks((current) => [...current.filter((item) => item.id !== value.id), value]), []);

  const refresh = useCallback(() => setGeneration((value) => value + 1), []);
  const refreshShelf = useCallback(async () => {
    if (!api) return;
    const result = await api.getShelf(sheetId, actionId);
    setItems(matchingShelf(result.items, outputColumns));
  }, [api, sheetId, actionId, outputColumns]);

  useEffect(() => {
    if (!api) return;
    let alive = true;
    void Promise.all([api.getLinks(sheetId), api.getShelf(sheetId, actionId)]).then(([linkResult, shelfResult]) => {
      if (alive) { setLinks(linkResult.links); setItems(matchingShelf(shelfResult.items, outputColumns)); }
    }, (problem) => { if (alive) setError(String(problem.message ?? problem)); });
    return () => { alive = false; };
  }, [api, sheetId, actionId, generation, dataRevision, outputColumns]);

  useEffect(() => {
    const current = ++sequence.current;
    if (!api || !request.variants.length) return;
    void api.score(request).then((result) => {
      if (current === sequence.current) { setScoreState({ key: scoreKey, requestKey, response: result }); setError(null); }
    }, (problem) => { if (current === sequence.current) { setScoreState({ key: scoreKey, requestKey, response: null }); setError(String(problem.message ?? problem)); } });
    return () => { sequence.current += 1; };
  }, [api, request, requestKey, scoreKey]);

  const mutate = useCallback(async <Value,>(operation: () => Promise<Value>): Promise<Value | undefined> => {
    setSaving(true); setError(null);
    try { const result = await operation(); refresh(); return result; }
    catch (problem) { setError(problem instanceof Error ? problem.message : String(problem)); return undefined; }
    finally { setSaving(false); }
  }, [refresh]);

  return { api, link, links, request, response, items, loading, saving, error, refresh, refreshShelf, mutate,
    setResponse, setError, setLink };
}
