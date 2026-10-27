import { AlertTriangle } from 'lucide-react';
import type { PdfImportCategory, PdfImportOcrPolicy } from '../../../api/pdfImports';
import { count } from './model';

/** Shared four-policy choice for uploaded files and existing PDF rows. */
export function PdfOcrPolicy({ value, counts, checking, busy, onChange, pdfOnly = false }: {
  value: PdfImportOcrPolicy;
  counts: Record<PdfImportOcrPolicy, number>;
  checking: boolean;
  busy: boolean;
  onChange(value: PdfImportOcrPolicy): void;
  pdfOnly?: boolean;
}) {
  const options: Array<{ value: PdfImportOcrPolicy; label: string; detail: string }> = [
    { value: 'missing', label: 'Missing text', detail: pdfOnly ? 'PDFs without text' : 'PDFs without text and all images' },
    { value: 'missing_or_suspicious', label: 'Missing or suspicious text', detail: pdfOnly ? 'Scans and text that may be garbled' : 'Scans, images, and text that may be garbled' },
    { value: 'all', label: pdfOnly ? 'All PDFs' : 'All PDFs and images', detail: pdfOnly ? 'Read every PDF with OCR' : 'Read every PDF and image with OCR' },
    { value: 'none', label: 'None', detail: 'Use text layers only' },
  ];
  return <fieldset className="pdf-batch-ocr-policy" aria-label="Apply OCR to">
    {options.map((option) => <label key={option.value} className={value === option.value ? 'active' : ''}>
      <input type="radio" name="pdf-batch-ocr-policy" value={option.value} disabled={busy} checked={value === option.value} onChange={() => onChange(option.value)} />
      <span><strong>{option.label}</strong><small>{option.detail}</small></span>
      <b>{count(counts[option.value], pdfOnly ? 'PDF' : 'file')}{checking ? ' so far' : ''}</b>
    </label>)}
    {value === 'none' && <p><AlertTriangle size={13} /> Files without a text layer won't be searchable until you run OCR on them.</p>}
  </fieldset>;
}

export function TextSourceBadge({ category, policy, textKind }: { category: PdfImportCategory; policy: PdfImportOcrPolicy; textKind?: 'native' | 'ocr' | null }) {
  if (textKind === 'ocr') return <span role="cell"><i className="pdf-batch-text-badge ocr">OCR</i></span>;
  if (textKind === 'native') return <span role="cell"><i className="pdf-batch-text-badge native">Text layer</i></span>;
  if (textKind === null && (category === 'text' || category === 'suspicious')) return <span role="cell"><i className="pdf-batch-text-badge neutral">Text available</i></span>;
  const canUseOcr = category === 'text' || category === 'suspicious' || category === 'missing' || category === 'image';
  const useOcr = canUseOcr && (policy === 'all'
    || ((policy === 'missing' || policy === 'missing_or_suspicious') && (category === 'missing' || category === 'image'))
    || (policy === 'missing_or_suspicious' && category === 'suspicious'));
  if (useOcr) return <span role="cell"><i className="pdf-batch-text-badge ocr">OCR</i></span>;
  if (category === 'text' || category === 'suspicious') return <span role="cell"><i className="pdf-batch-text-badge native">Text layer</i></span>;
  if (category === 'missing' || category === 'image') return <span role="cell"><i className="pdf-batch-text-badge neutral">No text</i></span>;
  return <span role="cell">—</span>;
}
