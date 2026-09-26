import { useEffect, useRef, useState } from 'react';
import { Download, Pencil, Trash2 } from 'lucide-react';
import type { AskThread } from '../../api/projectQA';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';

export function AskThreadControls({ thread, active }: { thread: AskThread; active: boolean }) {
  const { qa } = useWorkspaceStores();
  const [editing, setEditing] = useState<'rename' | 'delete' | null>(null);
  const [name, setName] = useState(thread.title);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { if (editing) dialog.current?.showModal(); }, [editing]);
  async function exportReport() {
    try {
      const report = await qa.report(thread.id);
      const url = URL.createObjectURL(new Blob([report.markdown], { type: 'text/markdown;charset=utf-8' }));
      const link = document.createElement('a');
      link.href = url; link.download = 'frisket-conversation.md'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch { setError('Could not download this conversation. Please try again.'); }
  }
  async function submit() {
    setBusy(true);
    try {
      const saved = editing === 'delete' ? await qa.deleteThread() : await qa.rename(name);
      if (saved) setEditing(null);
    } finally { setBusy(false); }
  }
  return <>
    <div className="ask-thread-actions">
      <button type="button" aria-label="Rename conversation" onClick={() => { setName(thread.title); setEditing('rename'); }}><Pencil size={14} /></button>
      <button type="button" aria-label="Download conversation" onClick={() => void exportReport()}><Download size={14} /></button>
      <button type="button" aria-label="Delete conversation" disabled={active} title={active ? 'Stop the investigation before deleting this conversation.' : undefined} onClick={() => setEditing('delete')}><Trash2 size={14} /></button>
    </div>
    {error && <p role="alert">{error}</p>}
    {editing && <dialog ref={dialog} className="modal-card ask-thread-dialog" aria-label={editing === 'rename' ? 'Rename conversation' : 'Delete conversation'} onCancel={(event) => { event.preventDefault(); if (!busy) setEditing(null); }}>
      <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
        <h2>{editing === 'rename' ? 'Rename conversation' : 'Delete conversation?'}</h2>
        {editing === 'rename' ? <input aria-label="Conversation name" className="form-input" value={name} maxLength={200} onChange={(event) => setName(event.target.value)} /> : <p>This removes the conversation and its citations for everyone in the project.</p>}
        <div className="form-actions"><button type="button" className="btn" disabled={busy} onClick={() => setEditing(null)}>Cancel</button><button type="submit" className="btn btn-primary" disabled={busy || editing === 'rename' && !name.trim()}>{editing === 'rename' ? 'Save' : 'Delete'}</button></div>
      </form>
    </dialog>}
  </>;
}
