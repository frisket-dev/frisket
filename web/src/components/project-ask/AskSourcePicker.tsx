import { useEffect, useRef, useState } from 'react';
import type { AskScope } from '../../api/projectQA';
import type { Row, SheetMeta } from '../../api/types';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { documentMediaColumns } from '../../workbench/documentMedia';
import { resolveMediaValue } from '../../media/resolveMediaValue';
import { PanelError, PanelLoading } from '../PanelPrimitives';

type Source = NonNullable<AskScope['sources']>[number];
const key = (source: Source) => JSON.stringify(source);

function SheetFiles({ sheet, selected, onToggle }: {
  sheet: SheetMeta; selected: Source[]; onToggle(source: Source): void;
}) {
  const { projectApi, chromePreferences: { projectId } } = useWorkspaceStores();
  const [rows, setRows] = useState<Row[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const columns = documentMediaColumns(sheet);
  useEffect(() => {
    let alive = true;
    void projectApi.getSheetData(sheet.id, 0, 50).then((page) => {
      if (alive) { setRows(page.rows); setTotal(page.total); setLoading(false); }
    }).catch(() => { if (alive) { setError('Could not load these files.'); setLoading(false); } });
    return () => { alive = false; };
  }, [projectApi, sheet.id]);
  async function more() {
    setLoading(true);
    try {
      const page = await projectApi.getSheetData(sheet.id, rows.length, 50);
      setRows((current) => [...current, ...page.rows]); setTotal(page.total);
    } catch { setError('Could not load more files.'); }
    finally { setLoading(false); }
  }
  return <div className="ask-file-list">
    {error && <PanelError>{error}</PanelError>}
    {rows.flatMap((row) => columns.map((column) => {
      const media = resolveMediaValue(row.cells[column.id] ?? null, projectId);
      if (!media) return null;
      const source: Source = { kind: 'file', sheet_id: Number(sheet.id), row_id: Number(row.id), column_id: Number(column.id) };
      const checked = selected.some((s) => s.kind === 'sheet' && s.sheet_id === source.sheet_id || key(s) === key(source));
      return <label key={`${row.id}:${column.id}`}><input type="checkbox" checked={checked} disabled={selected.some((s) => s.kind === 'sheet' && s.sheet_id === source.sheet_id)} onChange={() => onToggle(source)} />{media.filename ?? media.label ?? `${column.name} · row ${row.id}`}</label>;
    }))}
    {loading && <PanelLoading label="Loading files…" />}
    {!loading && rows.length < total && <button type="button" className="btn" onClick={() => void more()}>More files</button>}
    {!loading && !rows.length && <p>No files in this sheet.</p>}
  </div>;
}

export function AskSourcePicker({ scope, sheets, onApply, onClose }: {
  scope: AskScope | null; sheets: SheetMeta[]; onApply(scope: AskScope): void; onClose(): void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [wholeProject, setWholeProject] = useState(scope?.kind === 'project');
  const [sources, setSources] = useState<Source[]>(scope?.sources ?? []);
  const [expanded, setExpanded] = useState<string | null>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  function toggle(source: Source) {
    setWholeProject(false);
    setSources((current) => {
      if (current.some((item) => key(item) === key(source))) return current.filter((item) => key(item) !== key(source));
      return [...current.filter((item) => source.kind !== 'sheet' || item.sheet_id !== source.sheet_id), source];
    });
  }
  return <dialog ref={dialog} className="modal-card ask-source-picker" aria-labelledby="ask-source-title" onCancel={(event) => { event.preventDefault(); onClose(); }}>
    <form onSubmit={(event) => { event.preventDefault(); onApply(wholeProject ? { kind: 'project' } : { kind: 'sources', sources }); }}>
      <h2 id="ask-source-title">Choose project sources</h2>
      <p>Choose sheets or files already in this project.</p>
      <label><input type="checkbox" checked={wholeProject} onChange={(event) => setWholeProject(event.target.checked)} />Whole project</label>
      <div className="ask-source-options">
        {sheets.map((sheet) => <div key={sheet.id}>
          <div className="ask-source-sheet"><label><input type="checkbox" checked={wholeProject || sources.some((s) => s.kind === 'sheet' && s.sheet_id === Number(sheet.id))} disabled={wholeProject} onChange={() => toggle({ kind: 'sheet', sheet_id: Number(sheet.id) })} />{sheet.name}</label>
            {documentMediaColumns(sheet).length > 0 && <button type="button" className="btn" aria-expanded={expanded === sheet.id} onClick={() => setExpanded(expanded === sheet.id ? null : sheet.id)}>Files</button>}
          </div>
          {!wholeProject && sources.filter((s) => s.kind === 'rows' && s.sheet_id === Number(sheet.id)).map((source) => <label key={key(source)}><input type="checkbox" checked onChange={() => toggle(source)} />{source.kind === 'rows' ? source.row_ids.length : 0} selected rows</label>)}
          {expanded === sheet.id && !wholeProject && <SheetFiles sheet={sheet} selected={sources} onToggle={toggle} />}
        </div>)}
      </div>
      <div className="form-actions"><button type="button" className="btn" onClick={onClose}>Cancel</button><button type="submit" className="btn btn-primary" disabled={!wholeProject && !sources.length}>Use sources</button></div>
    </form>
  </dialog>;
}
