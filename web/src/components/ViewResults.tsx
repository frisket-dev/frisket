import { useState } from 'react';
import type { View } from '../api/types';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useSelector } from '../bind/useSelector';
import { routePath } from '../routes';
import { Button } from './Button';
import { PanelError, PanelLoading } from './PanelPrimitives';
import { useLoader } from '../hooks/useLoader';

/** Project-wide views use paginated results; they never pretend to be one sheet. */
export function ViewResults({ view, onClose }: { view: View; onClose(): void }) {
  const { projectApi, projectData, chromePreferences: { projectId } } = useWorkspaceStores();
  const sheets = useSelector(projectData.store, (state) => state.sheets);
  const [offset, setOffset] = useState(0);
  const resolved = useLoader(
    () => projectApi.resolveView(view.id, { offset, limit: 25 }),
    [projectApi, view.id, offset],
    { keepPreviousData: true },
  );
  const page = resolved.data?.view_id === view.id && resolved.data.offset === offset ? resolved.data : null;
  return <div className="panel-card content-stack">
    <div className="control-row"><strong className="panel-item-main">{view.name}</strong><Button onClick={onClose}>Close</Button></div>
    {resolved.error ? <PanelError>{resolved.error}</PanelError> : !page ? <PanelLoading label="Loading matching rows…" /> : <>
      <span className="form-hint">{page.total.toLocaleString()} matching rows</span>
      <ul className="panel-list">{page.refs.map((ref) => <li key={`${ref.sheet_id}:${ref.row_id}`}>
        <a href={routePath({ kind: 'project', projectId, sheetId: String(ref.sheet_id), panel: { kind: 'row', rowId: String(ref.row_id) } })}>
          {sheets.find((sheet) => Number(sheet.id) === ref.sheet_id)?.name ?? `Sheet ${ref.sheet_id}`} · row {ref.row_id}
        </a>
      </li>)}</ul>
      <div className="control-row"><Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous</Button>
        <Button disabled={offset + page.refs.length >= page.total} onClick={() => setOffset(offset + 25)}>Next</Button></div>
    </>}
  </div>;
}
