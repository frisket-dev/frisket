import { useEffect, useState } from 'react';
import type { SavedView, ViewResolved } from '../api/types';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useSelector } from '../bind/useSelector';
import { routePath } from '../routes';
import { Button } from './Button';
import { PanelError, PanelLoading } from './PanelPrimitives';

/** Project-wide views use paginated results; they never pretend to be one sheet. */
export function ViewResults({ view, onClose }: { view: SavedView; onClose(): void }) {
  const { projectApi, projectData, chromePreferences: { projectId } } = useWorkspaceStores();
  const sheets = useSelector(projectData.store, (state) => state.sheets);
  const [offset, setOffset] = useState(0);
  const [state, setState] = useState<{ page?: ViewResolved; error?: string }>({});
  useEffect(() => {
    let active = true;
    void projectApi.resolveView(view.id, { offset, limit: 25 }).then((page) => {
      if (active) setState({ page });
    }).catch((error: Error) => { if (active) setState({ error: error.message }); });
    return () => { active = false; };
  }, [projectApi, view.id, offset]);
  const page = state.page?.view_id === view.id && state.page.offset === offset ? state.page : null;
  return <div className="panel-card content-stack">
    <div className="control-row"><strong className="panel-item-main">{view.name}</strong><Button onClick={onClose}>Close</Button></div>
    {state.error ? <PanelError>{state.error}</PanelError> : !page ? <PanelLoading label="Loading matching rows…" /> : <>
      <span className="form-hint">{page.total.toLocaleString()} matching rows</span>
      <ul className="panel-list">{page.refs.map((ref) => <li key={`${ref.sheet_id}:${ref.row_id}`}>
        <a href={routePath({ kind: 'project', projectId, sheetId: String(ref.sheet_id), panel: { kind: 'row', rowId: String(ref.row_id) } })}>
          {sheets.find((sheet) => Number(sheet.id) === ref.sheet_id)?.name ?? `Sheet ${ref.sheet_id}`} · row {ref.row_id}
        </a>
      </li>)}</ul>
      <div className="control-row"><Button disabled={offset === 0} onClick={() => { setState({}); setOffset(Math.max(0, offset - 25)); }}>Previous</Button>
        <Button disabled={offset + page.refs.length >= page.total} onClick={() => { setState({}); setOffset(offset + 25); }}>Next</Button></div>
    </>}
  </div>;
}
