import { useEffect, useState } from 'react';
import { readDocumentTextResource } from '../api/raw/blobText';

/** How much of a text file the reader will render. Past this the DOM cost stops
 *  being about reading and starts being about scrolling; the tail is announced
 *  rather than silently dropped, and the download link stays reachable. */
const TEXT_FILE_RENDER_LIMIT = 500_000;

/** A `text/*` blob rendered as its own contents. Before this, every text file
 *  fell into DocumentReader's `other` branch and showed a download-link icon — the
 *  Document view could open a PDF, an image, a video and an audio file, and
 *  not a .txt.
 *
 *  This is the FILE reader, not the annotated-cell reader: a blob has no cell
 *  coordinate surface, so it carries no entity marks: a cell surface requires
 *  the producer's exact string to equal one stored cell. It shares only the
 *  `.document-text` presentation. */
export function TextFileViewer({ url, label }: { url: string; label: string }) {
  const [state, setState] = useState<{
    text: string | null;
    truncated: boolean;
    error: string | null;
  }>({ text: null, truncated: false, error: null });

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    readDocumentTextResource(url, { signal: controller.signal })
      .then((body) => {
        if (cancelled) return;
        setState({
          text: body.slice(0, TEXT_FILE_RENDER_LIMIT),
          truncated: body.length > TEXT_FILE_RENDER_LIMIT,
          error: null,
        });
      })
      .catch((err: unknown) => {
        if (cancelled || controller.signal.aborted) return;
        setState({
          text: null,
          truncated: false,
          error: err instanceof Error ? err.message : 'Could not load this file.',
        });
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [url]);

  return (
    <div className="document-textfile" data-testid="document-text-file" data-truncated={state.truncated ? 'true' : 'false'}>
      {state.error !== null && (
        <div className="document-pdf-error" role="alert" data-testid="document-text-file-error">
          {state.error}{' '}
          <a href={url} target="_blank" rel="noopener noreferrer">
            {label}
          </a>
        </div>
      )}
      {state.text !== null && (
        <>
          <pre className="document-text" data-testid="document-text-body">
            {state.text}
          </pre>
          {state.truncated && (
            <p className="document-text-truncated muted" data-testid="document-text-truncated">
              Showing the first {TEXT_FILE_RENDER_LIMIT.toLocaleString()} characters.{' '}
              <a href={url} target="_blank" rel="noopener noreferrer">
                Download {label}
              </a>{' '}
              for the rest.
            </p>
          )}
        </>
      )}
    </div>
  );
}
