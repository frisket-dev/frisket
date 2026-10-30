import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { EvaluationApi, EvaluationLink, EvaluationResponse, SavedSummary } from '../../api/evaluation';
import type { ActionCompareSessionSnapshot } from '../actionCompareSession';
import { comparisonRequest } from './comparisonEvaluation';

export function useComparisonEvaluation(api: EvaluationApi | undefined, state: ActionCompareSessionSnapshot) {
  const [links, setLinks] = useState<EvaluationLink[]>([]);
  const [items, setItems] = useState<SavedSummary[]>([]);
  const [response, setResponse] = useState<EvaluationResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);
  const sequence = useRef(0);
  const actionId = state.context.draft.action_id;
  const sheetId = state.context.sheet.id;
  const outputIds = useMemo(() => new Set(state.context.output_columns.flatMap((column) => column.column_id ? [column.column_id] : [])), [state.context.output_columns]);
  const link = links.find((candidate) => candidate.action_id === actionId
    && candidate.fields.some((field) => outputIds.has(field.output_column_id))) ?? null;
  const request = useMemo(() => comparisonRequest(state, link?.id ?? null), [state, link?.id]);
  const requestKey = JSON.stringify(request);
  const requestRef = useRef(request);
  requestRef.current = request;

  const refresh = useCallback(() => setGeneration((value) => value + 1), []);
  const refreshShelf = useCallback(async () => {
    if (!api) return;
    const result = await api.getShelf(sheetId, actionId);
    const outputs = state.context.output_columns.map((column) => column.logical_key).sort().join('\0');
    setItems(result.items.filter((item) => item.output_columns.map((column) => column.logical_key).sort().join('\0') === outputs));
  }, [api, sheetId, actionId, state.context.output_columns]);

  useEffect(() => {
    if (!api) return;
    let alive = true;
    void api.getLinks(sheetId).then((result) => { if (alive) setLinks(result.links); }, (problem) => { if (alive) setError(String(problem.message ?? problem)); });
    void refreshShelf().catch((problem) => { if (alive) setError(String(problem.message ?? problem)); });
    return () => { alive = false; };
  }, [api, sheetId, generation, refreshShelf]);

  useEffect(() => {
    const current = ++sequence.current;
    if (!api || !requestRef.current.variants.length) { setResponse(null); setLoading(false); return; }
    setLoading(true); setError(null);
    void api.score(requestRef.current).then((result) => {
      if (current === sequence.current) setResponse(result);
    }, (problem) => { if (current === sequence.current) { setResponse(null); setError(String(problem.message ?? problem)); } })
      .finally(() => { if (current === sequence.current) setLoading(false); });
    return () => { sequence.current += 1; };
  }, [api, requestKey, generation]);

  const mutate = useCallback(async <Value,>(operation: () => Promise<Value>): Promise<Value | undefined> => {
    setSaving(true); setError(null);
    try { const result = await operation(); refresh(); return result; }
    catch (problem) { setError(problem instanceof Error ? problem.message : String(problem)); return undefined; }
    finally { setSaving(false); }
  }, [refresh]);

  return { api, link, links, request, response, items, loading, saving, error, refresh, refreshShelf, mutate,
    setResponse, setError, setLink: (value: EvaluationLink) => setLinks((current) => [...current.filter((item) => item.id !== value.id), value]) };
}
