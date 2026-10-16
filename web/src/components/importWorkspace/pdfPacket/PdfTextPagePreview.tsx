import { LoaderCircle } from 'lucide-react';

export function PdfTextPagePreview({
  page,
  thumbnailUrl,
  thumbnailAlt = `Page ${page}`,
  heading = 'Extracted text',
  text,
  emptyCopy = 'No extracted text on this page.',
  loading = false,
  loadingCopy = 'Reading page…',
  truncated = false,
}: {
  page: number;
  thumbnailUrl?: string | null;
  thumbnailAlt?: string;
  heading?: string;
  text: string;
  emptyCopy?: string;
  loading?: boolean;
  loadingCopy?: string;
  truncated?: boolean;
}) {
  return (
    <div className="pdf-packet-page-and-text">
      {thumbnailUrl
        ? <img src={thumbnailUrl} alt={thumbnailAlt} />
        : <div className="pdf-text-preview-placeholder" aria-label={thumbnailAlt}>Page {page}</div>}
      <div className="pdf-packet-text-card">
        <strong>{heading}</strong>
        {loading ? (
          <div className="pdf-packet-text-loading" role="status" aria-label={loadingCopy}>
            <LoaderCircle className="spin" size={24} aria-hidden />
            <span>{loadingCopy}</span>
          </div>
        ) : text ? (
          <>
            <pre>{text}</pre>
            {truncated && <span className="form-hint">Preview shortened for this page.</span>}
          </>
        ) : <p>{emptyCopy}</p>}
      </div>
    </div>
  );
}
