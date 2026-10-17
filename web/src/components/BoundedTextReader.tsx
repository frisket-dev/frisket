import { useEffect, useRef, useState } from 'react';

export interface TextWindow {
  retrieval_revision: number;
  total_characters: number;
  total_bytes: number;
  range: { start: number; end: number };
  text: string;
  previous_start: number | null;
  next_start: number | null;
}

export interface BoundedTextReaderProps {
  initialRevision?: number;
  totalCharacters: number;
  totalBytes: number;
  loadWindow(options: { start: number; limit: number; revision?: number; signal: AbortSignal }): Promise<TextWindow>;
  downloadUrl(revision: number): string;
  copyFullValue(revision: number, signal: AbortSignal): Promise<string>;
}

const WINDOW_SIZE = 16_384;

/** Plain text for bounded cell/blob reads, including Markdown and encoded JSON. */
export function BoundedTextReader({
  initialRevision, totalCharacters, totalBytes, loadWindow, downloadUrl, copyFullValue,
}: BoundedTextReaderProps) {
  const [request, setRequest] = useState({ start: 0, revision: initialRevision, attempt: 0 });
  const requestKey = `${request.start}:${request.revision ?? ''}:${request.attempt}`;
  const [result, setResult] = useState<{ key: string; window?: TextWindow; error?: string }>({ key: '' });
  const [jump, setJump] = useState('');
  const [copyStatus, setCopyStatus] = useState('');
  const copyController = useRef<AbortController | null>(null);
  useEffect(() => () => { copyController.current?.abort(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    void loadWindow({ start: request.start, limit: WINDOW_SIZE, revision: request.revision, signal: controller.signal })
      .then((window) => { if (!controller.signal.aborted) setResult({ key: requestKey, window }); })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setResult({ key: requestKey,
          error: typeof error === 'object' && error !== null && 'status' in error && error.status === 409
            ? 'This value changed. Reload to read its current version.'
            : error instanceof Error ? error.message : 'Could not load text.' });
      });
    return () => controller.abort();
  }, [loadWindow, request.start, request.revision, requestKey]);
  const current = result.key === requestKey ? result : null;
  const window = current?.window;
  const total = window?.total_characters ?? totalCharacters;
  const bytes = window?.total_bytes ?? totalBytes;
  const busy = current === null;
  const revision = window?.retrieval_revision;
  const go = (start: number) => {
    if (!window) return;
    setRequest({ start, revision: window.retrieval_revision, attempt: request.attempt + 1 });
  };
  async function copy() {
    if (revision === undefined) return;
    copyController.current?.abort();
    const controller = new AbortController();
    copyController.current = controller;
    setCopyStatus('Copying full value…');
    try {
      const exact = await copyFullValue(revision, controller.signal);
      if (controller.signal.aborted) return;
      await navigator.clipboard.writeText(exact);
      if (!controller.signal.aborted) setCopyStatus('Full value copied.');
    } catch (error) {
      if (!controller.signal.aborted) setCopyStatus(error instanceof Error ? error.message : 'Could not copy full value.');
    }
  }
  return (
    <section className="bounded-text-reader" data-testid="bounded-text-reader" aria-label="Large value reader">
      <p className="muted">{total.toLocaleString()} characters · {bytes.toLocaleString()} bytes. Read-only above 64 KiB.</p>
      <div className="bounded-text-actions" style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <button className="mini-btn" type="button" disabled={!window || busy || window.range.start === 0} onClick={() => go(0)}>First</button>
        <button className="mini-btn" type="button" disabled={!window || busy || window.previous_start === null} onClick={() => window?.previous_start != null && go(window.previous_start)}>Previous</button>
        <button className="mini-btn" type="button" disabled={!window || busy} onClick={() => go(Math.floor(total / 2))}>Middle</button>
        <button className="mini-btn" type="button" disabled={!window || busy || window.next_start === null} onClick={() => window?.next_start != null && go(window.next_start)}>Next</button>
        <button className="mini-btn" type="button" disabled={!window || busy} onClick={() => go(Math.max(0, total - WINDOW_SIZE))}>Last</button>
      </div>
      <form onSubmit={(event) => {
        event.preventDefault();
        const character = Number(jump);
        if (Number.isInteger(character) && character >= 1 && character <= total) go(character - 1);
      }} style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBlock: 8 }}>
        <label>Character <input aria-label="Go to character" type="number" min="1" max={total} style={{ width: '9ch' }} value={jump} onChange={(event) => setJump(event.target.value)} /></label>
        <button type="submit" className="mini-btn" disabled={!window || busy || !jump}>Go</button>
      </form>
      {busy ? <p role="status">Loading text…</p> : current?.error ? (
        <div role="alert"><p>{current.error}</p><button type="button" className="mini-btn" onClick={() => {
          setRequest({ start: 0, revision: undefined, attempt: request.attempt + 1 });
        }}>Reload text</button></div>
      ) : window ? <>
        <p className="muted" role="status">Characters {window.range.start + 1}–{window.range.end} of {total.toLocaleString()} · plain text excerpt</p>
        <pre data-testid="bounded-text-window" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: '45vh', overflowY: 'auto' }}>{window.text}</pre>
      </> : null}
      {revision !== undefined && <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <a className="mini-btn" href={downloadUrl(revision)} download>Download full value</a>
        <button type="button" className="mini-btn" onClick={() => void copy()}>Copy full value ({bytes.toLocaleString()} bytes)</button>
      </div>}
      <p role="status">{copyStatus}</p>
    </section>
  );
}
