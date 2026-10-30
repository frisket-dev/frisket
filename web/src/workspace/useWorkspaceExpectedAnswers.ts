import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createActionComparisonApi, type ActionComparisonContext } from '../api/actionComparison';
import { ApiError, actionPreviewRunErrorFromContract } from '../api/contractErrors';
import { createEvaluationApi, type EvaluationLink, type EvaluationLinkInput, type EvaluationRule, type ExpectedProjection } from '../api/evaluation';
import type { ActionCatalogEntry, ActionTemplate, CellValue, JsonValue, SheetMeta } from '../api/types';
import { compatibleSourceColumns, primaryColumnRequirement } from '../actions/model';
import { actionComparisonKind } from '../grid/actionComparisonCatalog';
import type { ExpectedGridColumn } from '../grid/SheetGrid';
import type { ActionRunAnchor } from '../grid/actionRunEntry';
import type { GridApiPort } from '../api/ports';
import { isExpectedLinkAvailable } from './expectedLinkAvailability';
import { MEDIA_COMPARE_COMMAND_BY_ACTION } from '../workbench/mediaCompareSelector';

const EMPTY_LINKS: EvaluationLink[] = [];
const EMPTY_COUNTS: Record<number, number> = {};
const EMPTY_RUNS: Run[] = [];
interface Run { anchor: ActionRunAnchor; context: ActionComparisonContext }
interface RunDiscovery { runs: Run[]; retryable: boolean }
export function useWorkspaceExpectedAnswers(projectId: string, sheet: SheetMeta | undefined, sheets: SheetMeta[],
  projectApi: GridApiPort, entries: ActionCatalogEntry[] | undefined, templates: ActionTemplate[], dataVersion: number,
  showError: (message: string) => void) {
  const api = useMemo(() => createEvaluationApi(actionPreviewRunErrorFromContract, projectId), [projectId]);
  const comparisonApi = useMemo(() => createActionComparisonApi(actionPreviewRunErrorFromContract, projectId), [projectId]);
  const [linkResult, setLinkResult] = useState<{ owner: string; links: EvaluationLink[]; counts: Record<number, number> } | null>(null);
  const [currentRunResult, setCurrentRunResult] = useState<{ key: string; projectId: string; runs: Run[] } | null>(null);
  const [otherRunResult, setOtherRunResult] = useState<{ key: string; projectId: string; runs: Run[]; join: object } | null>(null);
  const currentDiscovery = useRef<{ key: string; projectId: string; retryable: boolean } | null>(null);
  const otherDiscovery = useRef<{ key: string; projectId: string; join: object; retryable: boolean } | null>(null);
  const [revision, setRevision] = useState(0);
  const [join, setJoin] = useState<{ actionId?: string; link?: EvaluationLink; step?: 'columns' } | null>(null);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  // Stable identities drive discovery. An in-progress run can expose its output
  // columns before its comparison context is ready; retry that 409 after the
  // existing project-data invalidation, including the run completion signal.
  const identity = (target: SheetMeta) => ({ id: target.id, rowCount: target.rowCount,
    anchors: target.columns.filter(column => column.latestRunId)
      .filter((column, index, all) => all.findIndex(other => other.latestRunId === column.latestRunId) === index)
      .map(column => ({ sheetId: target.id, columnId: column.id, runId: column.latestRunId! })) });
  const sheetId = sheet?.id;
  const currentSignature = JSON.stringify(sheet ? [identity(sheet)] : []);
  const otherSignature = JSON.stringify(join ? sheets.filter(item => item.id !== sheetId).map(identity) : []);
  const showErrorRef = useRef(showError);
  useEffect(() => { showErrorRef.current = showError; }, [showError]);
  const readRuns = useCallback(async (signature: string): Promise<RunDiscovery> => {
    let retryable = false;
    const targets = JSON.parse(signature) as Array<{ id: string; rowCount: number; anchors: ActionRunAnchor[] }>;
    const groups = await Promise.all(targets.map(async target => {
      if (!target.anchors.length || !target.rowCount) return [];
      try {
        const data = await projectApi.getSheetData(target.id, 0, 1, { columnIds: [], valueMode: 'bounded' });
        const rowIds = data.rows.map(row => Number(row.id));
        if (!rowIds.length) return [];
        return (await Promise.all(target.anchors.map(async anchor => {
          try {
            const context = await comparisonApi.getContext({ source: { kind: 'run', run_id: Number(anchor.runId), column_id: Number(anchor.columnId) }, sample_row_ids: rowIds });
            return context.available ? { anchor, context } : null;
          } catch (error) {
            if (error instanceof ApiError && error.status === 409) retryable = true;
            return null;
          }
        }))).filter((item): item is Run => item !== null);
      } catch { return []; }
    }));
    return { runs: groups.flat(), retryable };
  }, [projectApi, comparisonApi]);
  const linksOwner = JSON.stringify([projectId, sheetId ?? null]);
  const linksKey = JSON.stringify([projectId, sheetId ?? null, revision]);
  useEffect(() => {
    let live = true;
    if (sheetId) void api.getLinks(Number(sheetId)).then(result => {
      if (live) setLinkResult(previous => ({ owner: linksOwner, links: result.links, counts: previous?.owner === linksOwner ? previous.counts : {} }));
      void Promise.all(result.links.filter(isExpectedLinkAvailable).map(async link => {
        try { const preview = await api.previewLink({ target_sheet_id: link.target_sheet_id, expected_sheet_id: link.expected_sheet_id, action_id: link.action_id, keys: link.keys, fields: link.fields, hidden: link.hidden }); return [link.id, preview.matched] as const; } catch { return null; }
      })).then(counts => { if (live) setLinkResult({ owner: linksOwner, links: result.links, counts: Object.fromEntries(counts.filter((item): item is readonly [number, number] => item !== null)) }); });
    }, error => { if (live) showErrorRef.current(error instanceof Error ? error.message : 'Could not load expected answers.'); });
    return () => { live = false; };
  }, [api, sheetId, linksKey, linksOwner]);
  useEffect(() => {
    let live = true;
    const previous = currentDiscovery.current;
    if (!previous || previous.key !== currentSignature || previous.projectId !== projectId || previous.retryable) {
      void readRuns(currentSignature).then(result => {
        if (live) {
          currentDiscovery.current = { key: currentSignature, projectId, retryable: result.retryable };
          setCurrentRunResult({ key: currentSignature, projectId, runs: result.runs });
        }
      });
    }
    return () => { live = false; };
  }, [readRuns, currentSignature, projectId, dataVersion]);
  useEffect(() => {
    let live = true;
    const previous = otherDiscovery.current;
    if (join && (!previous || previous.key !== otherSignature || previous.projectId !== projectId || previous.join !== join || previous.retryable)) {
      void readRuns(otherSignature).then(result => {
        if (live) {
          otherDiscovery.current = { key: otherSignature, projectId, join, retryable: result.retryable };
          setOtherRunResult({ key: otherSignature, projectId, runs: result.runs, join });
        }
      });
    }
    return () => { live = false; };
  }, [readRuns, otherSignature, join, projectId, dataVersion]);
  const links = linkResult?.owner === linksOwner ? linkResult.links : EMPTY_LINKS;
  const matchedCounts = linkResult?.owner === linksOwner ? linkResult.counts : EMPTY_COUNTS;
  const availableLinks = useMemo(() => links.filter(isExpectedLinkAvailable), [links]);
  const unavailableLinks = useMemo(() => links.filter(link => !isExpectedLinkAvailable(link)), [links]);
  const currentRuns = currentRunResult?.projectId === projectId && currentRunResult.key === currentSignature ? currentRunResult.runs : EMPTY_RUNS;
  const otherRuns = join && otherRunResult?.projectId === projectId && otherRunResult.join === join && otherRunResult.key === otherSignature ? otherRunResult.runs : EMPTY_RUNS;
  const runs = useMemo(() => [...currentRuns, ...otherRuns], [currentRuns, otherRuns]);
  const runChoices = useMemo(() => runs.filter(item => item.anchor.sheetId === sheet?.id).map(({ anchor, context }) => {
    const link = availableLinks.find(item => item.action_id === context.draft.action_id && item.fields.some(field => context.output_columns.some(output => output.column_id === field.output_column_id)));
    return { anchor, group: context.draft.action_id.startsWith('media.') ? 'media' as const : 'rows' as const, label: entries?.find(item => item.kind === context.draft.action_id)?.title ?? context.draft.action_id,
      ...(link ? { expected: { sheetName: link.expected_sheet_name, rowCount: matchedCounts[link.id] ?? 0 } } : {}) };
  }), [runs, availableLinks, entries, matchedCounts, sheet?.id]);
  const actionChoices = useMemo(() => (entries ?? []).filter(entry => actionComparisonKind(entry) || entry.kind in MEDIA_COMPARE_COMMAND_BY_ACTION).map(entry => {
    const template = templates.find(item => item.kind === entry.kind);
    const requirement = template ? primaryColumnRequirement(template) : null;
    const compatible = sheet && (!requirement || compatibleSourceColumns(sheet.columns, requirement).length > 0);
    return { kind: entry.kind, label: entry.title, group: (entry.kind in MEDIA_COMPARE_COMMAND_BY_ACTION && entry.kind !== 'map.translate') ? 'media' as const : 'rows' as const,
      ...(!sheet ? { disabledReason: 'Choose a sheet' } : !compatible ? { disabledReason: `Needs ${requirement?.label ?? 'a compatible column'}` } : entry.ui_hints.unavailable_reason ? { disabledReason: entry.ui_hints.unavailable_reason } : entry.ui_hints.missing_credentials?.length ? { disabledReason: 'Set up credentials first' } : {}) };
  }), [entries, templates, sheet]);
  const joinSheets = useMemo(() => sheets.map(item => ({ id: Number(item.id), name: item.name, columns: item.columns.map(column => ({ id: Number(column.id), name: column.name })) })), [sheets]);
  const joinActions = useMemo(() => runs.map(({ anchor, context }) => ({ id: `${anchor.runId}:${anchor.columnId}`, actionId: context.draft.action_id,
    label: `${entries?.find(item => item.kind === context.draft.action_id)?.title ?? context.draft.action_id} · ${context.output_columns.map(column => column.current_name).join(', ')} · ${context.sheet.name}`,
    targetSheetId: context.sheet.id,
    outputs: context.output_columns.filter((column): column is typeof column & { column_id: number } => column.column_id !== null)
      .map(column => ({ id: column.column_id, name: column.current_name, logicalKey: column.logical_key }))
  })), [runs, entries]);
  const expectedColumns = useMemo<ExpectedGridColumn[]>(() => availableLinks.flatMap(link => link.fields.map(field => ({
    id: `expected:${link.id}:${field.expected_column_id}`, link_id: link.id,
    expected_column_id: field.expected_column_id, output_column_id: field.output_column_id,
    logical_key: field.logical_key, rule: field.rule, hidden: field.hidden || link.hidden, source: field.source,
    name: sheets.find(item => Number(item.id) === link.expected_sheet_id)?.columns.find(column => Number(column.id) === field.expected_column_id)?.name ?? field.logical_key,
    sourceColumn: sheets.find(item => Number(item.id) === link.expected_sheet_id)?.columns.find(column => Number(column.id) === field.expected_column_id),
    type: sheets.find(item => Number(item.id) === link.expected_sheet_id)?.columns.find(column => Number(column.id) === field.expected_column_id)?.type ?? 'text',
  }))), [availableLinks, sheets]);
  const projectExpected = useCallback((rowIds: number[]) => api.projectExpected(Number(sheet?.id), rowIds), [api, sheet?.id]);
  const editExpected = useCallback(async (rowId: string, column: ExpectedProjection['columns'][number], cell: ExpectedProjection['rows'][string][string] | undefined, value: CellValue) => {
    if (cell?.source_row_id != null) await projectApi.editCells([{ rowId: String(cell.source_row_id), columnId: String(cell.source_column_id), value }]);
    else {
      const link = links.find(item => item.id === column.link_id);
      const run = runs.find(item => item.context.draft.action_id === link?.action_id && item.context.output_columns.some(output => output.column_id === column.output_column_id));
      if (!run) throw new Error('The action for this expected answer is unavailable.');
      await api.saveExpectedAnswer({ basis: run.context.comparison_basis, link_id: column.link_id, row_id: Number(rowId), logical_key: column.logical_key, value: JSON.parse(JSON.stringify(value)) as JsonValue, rule: column.rule });
    }
    if (cell?.source_row_id == null) refresh();
  }, [api, links, projectApi, refresh, runs]);
  const setLink = useCallback(async (link: EvaluationLinkInput, id: number) => {
    const input: EvaluationLinkInput = { target_sheet_id: link.target_sheet_id, expected_sheet_id: link.expected_sheet_id,
      action_id: link.action_id, keys: link.keys, fields: link.fields, hidden: link.hidden };
    await api.saveLink(input, id); refresh();
  }, [api, refresh]);
  const openJoin = useCallback((actionId?: string, link?: EvaluationLink, step?: 'columns') => setJoin({ actionId, link, step }), []);
  const closeJoin = useCallback(() => setJoin(null), []);
  const saveLink = useCallback(async (input: EvaluationLinkInput, changedRules: Array<{ expected_column_id: number; rule: EvaluationRule }>) => {
    await api.saveLink(input, join?.link?.id);
    try {
      // Link updates intentionally preserve existing rules. Only explicit edits
      // may change their separate authority; untouched fields can be stale.
      if (join?.link) for (const change of changedRules) await api.setRule(change.expected_column_id, change.rule);
    } finally { refresh(); }
  }, [api, join, refresh]);
  const removeLink = useCallback(async (id: number) => { await api.deleteLink(id); refresh(); }, [api, refresh]);
  const setRule = useCallback(async (columnId: number, rule: EvaluationRule) => { await api.setRule(columnId, rule); refresh(); }, [api, refresh]);
  return useMemo(() => ({ api, revision, links, availableLinks, unavailableLinks, removeLink, runs, runChoices,
    actionChoices, joinSheets, joinActions, join, openJoin, closeJoin, saveLink, refresh, matchedCounts,
    expectedColumns, projectExpected, editExpected, setLink, setRule }),
  [api, revision, links, availableLinks, unavailableLinks, removeLink, runs, runChoices, actionChoices,
    joinSheets, joinActions, join, openJoin, closeJoin, saveLink, refresh, matchedCounts, expectedColumns,
    projectExpected, editExpected, setLink, setRule]);
}
