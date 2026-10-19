import './BoundedTextReader.css';
import { Button } from './Button';
import { useEffect, useRef, useState } from 'react';
import type { BlobTextWindow } from '../api/raw/blobText';

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
  coordinate?: 'characters';
  initialRevision?: number;
  /** Absolute Unicode codepoints; highlight=false indicates a coarse source locator. */
  initialRange?: { start: number; end: number; highlight?: boolean };
  totalCharacters: number;
  totalBytes: number;
  loadWindow(options: { start: number; limit: number; revision?: number; signal: AbortSignal }): Promise<TextWindow>;
  downloadUrl(revision: number): string;
  copyFullValue(revision: number, signal: AbortSignal): Promise<string>;
}

export interface BlobTextReaderProps {
  coordinate: 'bytes';
  loadWindow(options: { start: number; limit: number; revision?: number; signal: AbortSignal }): Promise<BlobTextWindow>;
  downloadUrl: string;
  copyFullValue(signal: AbortSignal): Promise<string>;
}

const WINDOW_SIZE = 16_384;

/** Plain text for bounded cell/blob reads, including Markdown and encoded JSON. */
export function BoundedTextReader(props: BoundedTextReaderProps | BlobTextReaderProps) {
  const { loadWindow } = props;
  const initialRevision = props.coordinate === 'bytes' ? undefined : props.initialRevision;
  const initialRange = props.coordinate === 'bytes' ? undefined : props.initialRange;
  const [request, setRequest] = useState({ start: initialRange ? Math.max(0, initialRange.start - 256) : 0,
    revision: initialRevision, attempt: 0, anchored: Boolean(initialRange) });
  const requestKey = `${request.start}:${request.revision ?? ''}:${request.attempt}`;
  const [result, setResult] = useState<{ key: string; window?: TextWindow | BlobTextWindow; error?: string }>({ key: '' });
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
  const total = props.coordinate === 'bytes'
    ? window?.total_bytes ?? 0
    : (window && 'total_characters' in window ? window.total_characters : props.totalCharacters);
  const bytes = window?.total_bytes ?? (props.coordinate === 'bytes' ? 0 : props.totalBytes);
  const busy = current === null;
  const revision = window && 'retrieval_revision' in window ? window.retrieval_revision : undefined;
  const go = (start: number) => {
    if (!window) return;
    setRequest({ start, revision, attempt: request.attempt + 1, anchored: request.anchored });
  };
  const highlightedText = window && request.anchored && initialRange && initialRange.highlight !== false
    && revision === initialRevision ? (() => {
      // Only this bounded passage is converted to codepoints, never the source value.
      const characters = Array.from(window.text);
      const start = Math.max(0, initialRange.start - window.range.start);
      const end = Math.min(characters.length, initialRange.end - window.range.start);
      return end > start ? <>{characters.slice(0, start).join('')}<mark data-testid="bounded-text-match">{characters.slice(start, end).join('')}</mark>{characters.slice(end).join('')}</> : window.text;
    })() : window?.text;
  async function copy() {
    if (props.coordinate !== 'bytes' && revision === undefined) return;
    copyController.current?.abort();
    const controller = new AbortController();
    copyController.current = controller;
    setCopyStatus(props.coordinate === 'bytes' ? 'Copying full file…' : 'Copying full value…');
    try {
      const exact = props.coordinate === 'bytes'
        ? await props.copyFullValue(controller.signal)
        : await props.copyFullValue(revision!, controller.signal);
      if (controller.signal.aborted) return;
      await navigator.clipboard.writeText(exact);
      if (!controller.signal.aborted) setCopyStatus(props.coordinate === 'bytes' ? 'Full file copied.' : 'Full value copied.');
    } catch (error) {
      if (!controller.signal.aborted) setCopyStatus(error instanceof Error ? error.message : 'Could not copy full value.');
    }
  }
  return (
    <section className="bounded-text-reader" data-testid="bounded-text-reader" aria-label="Large value reader">
      {props.coordinate !== 'bytes' && <p className="muted">{total.toLocaleString()} characters · {bytes.toLocaleString()} bytes. Read-only above 64 KiB.</p>}
      {(props.coordinate !== 'bytes' || !window || window.previous_start !== null || window.next_start !== null) && <div className="bounded-text-actions" style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <Button type="button" disabled={!window || busy || window.range.start === 0} onClick={() => go(0)}>First</Button>
        <Button type="button" disabled={!window || busy || window.previous_start === null} onClick={() => window?.previous_start != null && go(window.previous_start)}>Previous</Button>
        {props.coordinate !== 'bytes' && <Button type="button" disabled={!window || busy} onClick={() => go(Math.floor(total / 2))}>Middle</Button>}
        <Button type="button" disabled={!window || busy || window.next_start === null} onClick={() => window?.next_start != null && go(window.next_start)}>Next</Button>
        <Button type="button" disabled={!window || busy} onClick={() => go(props.coordinate === 'bytes' ? Math.floor(Math.max(0, total - 1) / WINDOW_SIZE) * WINDOW_SIZE : Math.max(0, total - WINDOW_SIZE))}>Last</Button>
      </div>}
      {props.coordinate !== 'bytes' && <form onSubmit={(event) => {
        event.preventDefault();
        const character = Number(jump);
        if (Number.isInteger(character) && character >= 1 && character <= total) go(character - 1);
      }} style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBlock: 8 }}>
        <label>Character <input aria-label="Go to character" type="number" min="1" max={total} style={{ width: '9ch' }} value={jump} onChange={(event) => setJump(event.target.value)} /></label>
        <Button type="submit" disabled={!window || busy || !jump}>Go</Button>
      </form>}
      {busy ? <p role="status">Loading text…</p> : current?.error ? (
        <div role="alert"><p>{current.error}</p><Button type="button" onClick={() => {
          setRequest({ start: 0, revision: undefined, attempt: request.attempt + 1, anchored: false });
        }}>Reload text</Button></div>
      ) : window ? <>
        <p className="muted" role="status">{props.coordinate === 'bytes'
          ? `Section ${Math.floor(('section_start' in window ? window.section_start : 0) / WINDOW_SIZE) + 1} of ${Math.max(1, Math.ceil(total / WINDOW_SIZE))} · plain text`
          : `Characters ${window.range.start + 1}–${window.range.end} of ${total.toLocaleString()} · plain text excerpt`}</p>
        <pre className={props.coordinate === 'bytes' ? 'document-text' : undefined} data-testid="bounded-text-window" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: '45vh', overflowY: 'auto' }}>{highlightedText}</pre>
      </> : null}
      {(props.coordinate === 'bytes' || revision !== undefined) && <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <a className="btn" href={props.coordinate === 'bytes' ? props.downloadUrl : props.downloadUrl(revision!)} download>{props.coordinate === 'bytes' ? 'Download full file' : 'Download full value'}</a>
        <Button type="button" disabled={!window || busy} onClick={() => void copy()}>Copy full {props.coordinate === 'bytes' ? 'file' : 'value'} ({bytes.toLocaleString()} bytes)</Button>
      </div>}
      <p role="status">{copyStatus}</p>
    </section>
  );
}
