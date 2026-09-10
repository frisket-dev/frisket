import { vi } from 'vitest';
import type { ComponentProps } from 'react';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { isGeneratedActionCatalogEntry } from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { servedActionCatalog } from './servedActionCatalog';
import { actionFormProjectApi } from './renderActionForm';
import { createWorkspaceTestHarness } from './workspaceTestHarness';
import { sheetMeta } from './actionFormFixtures';
import { columnDef } from './domainFixtures';

type Props = ComponentProps<typeof GeneratedActionForm>;
const catalog = servedActionCatalog();
export const joinLeft = sheetMeta([
  columnDef({ id: '11', name: 'code', type: 'text' }),
  columnDef({ id: '12', name: 'city', type: 'text' }),
  columnDef({ id: '13', name: 'state', type: 'text' }),
], { id: '1', name: 'Cities', rowCount: 5 });
export const joinRight = sheetMeta([
  columnDef({ id: '21', name: 'code', type: 'text' }),
  columnDef({ id: '22', name: 'city', type: 'text' }),
  columnDef({ id: '23', name: 'zone', type: 'text' }),
], { id: '2', name: 'Zones', rowCount: 10 });
export const joinApi = { ...actionFormProjectApi, listSheets: vi.fn(async () => [joinLeft, joinRight]) };
const harness = createWorkspaceTestHarness({ projectId: 'typed-join-form', api: { projectApi: joinApi } });

export function renderJoinForm(props: Partial<Props> = {}, actionId = 'derive.join') {
  const raw = catalog.actions.find((entry) => entry.kind === actionId);
  if (!raw || !isGeneratedActionCatalogEntry(raw)) throw new Error('Missing typed join catalog');
  const onExecute = vi.fn();
  const resolveParams = vi.fn<Props['resolveParams']>(async ({ params }) => {
    if (actionId === 'join.semantic') {
      const target = params.target as { sheet_id?: number; column?: string } | undefined;
      const carry = params.carry as string[] | undefined;
      const valid = params.source && target?.sheet_id && target.column && (carry ?? []).every(Boolean);
      return { diagnostics: valid ? {} : { source: { ok: false, message: 'Choose complete source references.' } },
        logical_outputs: valid ? [{ key: 'match_value', column_type: 'text' },
          { key: 'match_score', column_type: 'number' }, { key: 'matched_row_id', column_type: 'number' },
          { key: 'source', column_type: 'text' }, { key: 'carry.state', column_type: 'text' }] : [] };
    }
    const keys = params.join_keys as Array<{ left_column: string; right_column: string }> | undefined;
    const columns = params.columns as Array<{ column: string }> | null | undefined;
    const valid = keys?.length && keys.every((pair) => pair.left_column && pair.right_column)
      && (columns == null || columns.every((pick) => pick.column));
    return { diagnostics: valid ? {} : { join_keys: { ok: false, message: 'Choose complete source references.' } },
      logical_outputs: valid ? [{ key: 'code', column_type: 'text' },
        { key: 'city_left', column_type: 'text' }, { key: 'city_right', column_type: 'text' },
        { key: 'city_right_2', column_type: 'text' }, { key: '_merge', column_type: 'text' }] : [] };
  });
  const base: Props = { catalogEntry: raw, actionTemplate: generatedActionTemplateFromCatalogEntry(raw)!,
    sheet: joinLeft, running: false, onClose() {}, resolveParams, onExecute, ...props };
  return { ...harness.render(<GeneratedActionForm {...base} />), onExecute, resolveParams,
    entry: raw, props: base,
    rerenderForm(next: Partial<Props>) { return <GeneratedActionForm {...base} {...next} />; } };
}
