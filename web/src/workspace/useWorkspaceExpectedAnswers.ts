import { useCallback, useEffect, useMemo, useState } from 'react';
import { createActionComparisonApi, type ActionComparisonContext } from '../api/actionComparison';
import { actionPreviewRunErrorFromContract } from '../api/contractErrors';
import { createEvaluationApi, type EvaluationLink, type EvaluationLinkInput, type ExpectedProjection } from '../api/evaluation';
import type { ActionCatalogEntry, ActionTemplate, CellValue, JsonValue, SheetMeta } from '../api/types';
import { compatibleSourceColumns, primaryColumnRequirement } from '../actions/model';
import { actionComparisonKind } from '../grid/actionComparisonCatalog';
import type { ExpectedGridColumn } from '../grid/SheetGrid';
import type { ActionRunAnchor } from '../grid/actionRunEntry';
import type { GridApiPort } from '../api/ports';

interface Run { anchor: ActionRunAnchor; context: ActionComparisonContext }
export function useWorkspaceExpectedAnswers(projectId: string, sheet: SheetMeta | undefined, sheets: SheetMeta[],
  projectApi: GridApiPort, entries: ActionCatalogEntry[] | undefined, templates: ActionTemplate[], dataVersion: number,
  showError: (message: string) => void) {
  const api = useMemo(() => createEvaluationApi(actionPreviewRunErrorFromContract, projectId), [projectId]);
  const comparisonApi = useMemo(() => createActionComparisonApi(actionPreviewRunErrorFromContract, projectId), [projectId]);
  const [links, setLinks] = useState<EvaluationLink[]>([]);
  const [matchedCounts, setMatchedCounts] = useState<Record<number, number>>({});
  const [runs, setRuns] = useState<Run[]>([]);
  const [revision, setRevision] = useState(0);
  const [join, setJoin] = useState<{ actionId?: string; link?: EvaluationLink; step?: 'columns' } | null>(null);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  useEffect(() => {
    let live = true;
    setLinks([]); setRuns([]); setMatchedCounts({});
    if (!sheet) return () => { live = false; };
    void api.getLinks(Number(sheet.id)).then(result => {
      if (live) setLinks(result.links);
      void Promise.all(result.links.map(async link => {
        try { const preview = await api.previewLink({ target_sheet_id: link.target_sheet_id, expected_sheet_id: link.expected_sheet_id, action_id: link.action_id, keys: link.keys, fields: link.fields, hidden: link.hidden }); return [link.id, preview.matched] as const; } catch { return null; }
      })).then(counts => { if (live) setMatchedCounts(Object.fromEntries(counts.filter((item): item is readonly [number, number] => item !== null))); });
    }, error => { if (live) showError(error instanceof Error ? error.message : 'Could not load expected answers.'); });
    void Promise.all(sheets.map(async targetSheet => {
      const anchors = targetSheet.columns.filter(column => column.latestRunId).filter((column, index, all) => all.findIndex(other => other.latestRunId === column.latestRunId) === index)
        .map(column => ({ sheetId: targetSheet.id, columnId: column.id, runId: column.latestRunId! }));
      if (!anchors.length) return [];
      try {
        const data = await projectApi.getSheetData(targetSheet.id, 0, 1, { columnIds: [], valueMode: 'bounded' });
        const rowIds = data.rows.map(row => Number(row.id));
        if (!rowIds.length) return [];
        return (await Promise.all(anchors.map(async anchor => {
          try {
            const context = await comparisonApi.getContext({ source: { kind: 'run', run_id: Number(anchor.runId), column_id: Number(anchor.columnId) }, sample_row_ids: rowIds });
            return context.available ? { anchor, context } : null;
          } catch { return null; }
        }))).filter((item): item is Run => item !== null);
      } catch { return []; }
    })).then(groups => { if (live) setRuns(groups.flat()); });
    return () => { live = false; };
  }, [api, comparisonApi, projectApi, sheet, sheets, revision, dataVersion, showError]);
  const runChoices = useMemo(() => runs.filter(item => item.anchor.sheetId === sheet?.id).map(({ anchor, context }) => {
    const link = links.find(item => item.action_id === context.draft.action_id && item.fields.some(field => context.output_columns.some(output => output.column_id === field.output_column_id)));
    return { anchor, group: context.draft.action_id.startsWith('media.') ? 'media' as const : 'rows' as const, label: entries?.find(item => item.kind === context.draft.action_id)?.title ?? context.draft.action_id,
      ...(link ? { expected: { sheetName: link.expected_sheet_name, rowCount: matchedCounts[link.id] ?? 0 } } : {}) };
  }), [runs, links, entries, matchedCounts, sheet?.id]);
  const actionChoices = useMemo(() => (entries ?? []).filter(entry => actionComparisonKind(entry) || ['media.ocr', 'media.transcribe', 'map.find_topic_sections'].includes(entry.kind)).map(entry => {
    const template = templates.find(item => item.kind === entry.kind);
    const requirement = template ? primaryColumnRequirement(template) : null;
    const compatible = sheet && (!requirement || compatibleSourceColumns(sheet.columns, requirement).length > 0);
    return { kind: entry.kind, label: entry.title, group: (entry.kind === 'media.ocr' || entry.kind === 'media.transcribe' || entry.kind === 'map.find_topic_sections') ? 'media' as const : 'rows' as const,
      ...(!sheet ? { disabledReason: 'Choose a sheet' } : !compatible ? { disabledReason: `Needs ${requirement?.label ?? 'a compatible column'}` } : entry.ui_hints.unavailable_reason ? { disabledReason: entry.ui_hints.unavailable_reason } : entry.ui_hints.missing_credentials?.length ? { disabledReason: 'Set up credentials first' } : {}) };
  }), [entries, templates, sheet]);
  const joinSheets = useMemo(() => sheets.map(item => ({ id: Number(item.id), name: item.name, columns: item.columns.map(column => ({ id: Number(column.id), name: column.name })) })), [sheets]);
  const joinActions = useMemo(() => runs.map(({ anchor, context }) => ({ id: `${anchor.runId}:${anchor.columnId}`, actionId: context.draft.action_id,
    label: `${entries?.find(item => item.kind === context.draft.action_id)?.title ?? context.draft.action_id} · ${context.output_columns.map(column => column.current_name).join(', ')} · ${context.sheet.name}`,
    targetSheetId: context.sheet.id,
    outputs: context.output_columns.filter((column): column is typeof column & { column_id: number } => column.column_id !== null)
      .map(column => ({ id: column.column_id, name: column.current_name, logicalKey: column.logical_key }))
  })), [runs, entries]);
  const expectedColumns = useMemo<ExpectedGridColumn[]>(() => links.flatMap(link => link.fields.map(field => ({
    id: `expected:${link.id}:${field.expected_column_id}`, link_id: link.id,
    expected_column_id: field.expected_column_id, output_column_id: field.output_column_id,
    logical_key: field.logical_key, rule: field.rule, hidden: field.hidden || link.hidden, source: field.source,
    name: sheets.find(item => Number(item.id) === link.expected_sheet_id)?.columns.find(column => Number(column.id) === field.expected_column_id)?.name ?? field.logical_key,
    type: sheets.find(item => Number(item.id) === link.expected_sheet_id)?.columns.find(column => Number(column.id) === field.expected_column_id)?.type ?? 'text',
  }))), [links, sheets]);
  const projectExpected = useCallback((rowIds: number[]) => api.projectExpected(Number(sheet?.id), rowIds), [api, sheet?.id]);
  const editExpected = useCallback(async (rowId: string, column: ExpectedProjection['columns'][number], cell: ExpectedProjection['rows'][string][string] | undefined, value: CellValue) => {
    if (cell?.source_row_id != null) await projectApi.editCells([{ rowId: String(cell.source_row_id), columnId: String(cell.source_column_id), value }]);
    else {
      const link = links.find(item => item.id === column.link_id);
      const run = runs.find(item => item.context.draft.action_id === link?.action_id && item.context.output_columns.some(output => output.column_id === column.output_column_id));
      if (!run) throw new Error('The action for this expected answer is unavailable.');
      await api.saveExpectedAnswer({ basis: run.context.comparison_basis, link_id: column.link_id, row_id: Number(rowId), logical_key: column.logical_key, value: JSON.parse(JSON.stringify(value)) as JsonValue, rule: column.rule });
    }
    refresh();
  }, [api, links, projectApi, refresh, runs]);
  const setLink = useCallback(async (link: EvaluationLinkInput, id: number) => {
    const input: EvaluationLinkInput = { target_sheet_id: link.target_sheet_id, expected_sheet_id: link.expected_sheet_id,
      action_id: link.action_id, keys: link.keys, fields: link.fields, hidden: link.hidden };
    await api.saveLink(input, id); refresh();
  }, [api, refresh]);
  const openJoin = useCallback((actionId?: string, link?: EvaluationLink, step?: 'columns') => setJoin({ actionId, link, step }), []);
  const saveLink = useCallback(async (input: EvaluationLinkInput) => { await api.saveLink(input, join?.link?.id); refresh(); }, [api, join, refresh]);
  return { api, links, runs, runChoices, actionChoices, joinSheets, joinActions, join, openJoin, closeJoin: () => setJoin(null), saveLink, refresh, matchedCounts, expectedColumns, projectExpected, editExpected, setLink };
}
