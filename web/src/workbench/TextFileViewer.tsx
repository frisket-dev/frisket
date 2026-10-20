import './DocumentView.css';
import { useCallback } from 'react';
import { readDocumentTextResource, readDocumentTextWindow } from '../api/raw/blobText';
import { BoundedTextReader, type BlobTextReaderProps } from '../components/BoundedTextReader';

/** Original-file text uses bounded byte sections; full copy/download is explicit. */
export function TextFileViewer({ url, label }: { url: string; label: string }) {
  const loadWindow = useCallback<BlobTextReaderProps['loadWindow']>(
    ({ start, signal }) => readDocumentTextWindow(url, { start, signal }), [url]);
  const copyFullValue = useCallback((signal: AbortSignal) => readDocumentTextResource(url, { signal }), [url]);
  return <div className="document-textfile" data-testid="document-text-file" aria-label={label}>
    <BoundedTextReader key={url} coordinate="bytes" loadWindow={loadWindow}
      downloadUrl={url} copyFullValue={copyFullValue} />
  </div>;
}
