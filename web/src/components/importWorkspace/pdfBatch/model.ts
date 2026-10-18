import type {
  PdfImportCategory,
  PdfImportDestination,
  PdfImportFileSummary,
  PdfImportOcrPolicy,
  PdfImportSnapshot,
} from '../../../api/pdfImports';
import { formatNumberDisplay } from '../../../format';

export type PdfBatchStage = 'add' | 'check' | 'destination' | 'search';

function isPdf(file: File): boolean {
  return file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');
}

export function isJunkImportPath(path: string): boolean {
  const parts = path.replaceAll('\\', '/').split('/').filter(Boolean);
  if (parts.some((part) => part.toLowerCase() === '__macosx')) return true;
  const basename = (parts.at(-1) ?? '').toLowerCase();
  return basename === '.ds_store'
    || basename === 'thumbs.db'
    || basename === 'desktop.ini'
    || basename.startsWith('._')
    || basename.startsWith('~$')
    || (basename.startsWith('.~lock.') && basename.endsWith('#'));
}

export function filterPdfBatchFiles(files: readonly File[]): File[] {
  return files.filter((file) => !isJunkImportPath(file.webkitRelativePath || file.name));
}

export function canHandlePdfBatch(files: readonly File[]): boolean {
  return filterPdfBatchFiles(files).filter(isPdf).length >= 2;
}

export const PDF_BATCH_STAGES: ReadonlyArray<{ id: PdfBatchStage; label: string; testId: string }> = [
  { id: 'add', label: 'Add files', testId: 'pdf-batch-stage-add' },
  { id: 'check', label: 'Check text', testId: 'pdf-batch-stage-check' },
  { id: 'destination', label: 'Destination', testId: 'pdf-batch-stage-destination' },
  { id: 'search', label: 'Search setup', testId: 'pdf-batch-stage-search' },
];

export const PDF_CATEGORY_ORDER: readonly PdfImportCategory[] = [
  'text', 'suspicious', 'missing', 'image', 'other', 'unreadable', 'unchecked',
];

export const PDF_CHECK_CATEGORY_ORDER: readonly PdfImportCategory[] = [
  'suspicious', 'missing', 'image', 'text', 'other', 'unreadable',
];

export const PDF_CATEGORY_LABEL: Record<PdfImportCategory, string> = {
  text: 'PDFs with a text layer',
  suspicious: 'Text may be garbled',
  missing: 'PDFs with no text layer',
  image: 'Images',
  other: 'Other files',
  unreadable: "Couldn't read",
  unchecked: 'Not checked yet',
};

export const PDF_CATEGORY_TONE: Record<PdfImportCategory, string> = {
  text: '#1F7A5A',
  suspicious: '#C27C12',
  missing: '#4B4DDB',
  image: '#4B4DDB',
  other: '#A39A93',
  unreadable: '#B3412E',
  unchecked: '#A39A93',
};

export function visibleCategories(snapshot: PdfImportSnapshot): PdfImportCategory[] {
  return PDF_CATEGORY_ORDER.filter((category) => (snapshot.category_counts[category] ?? 0) > 0);
}

export function representativeFiles(
  files: readonly PdfImportFileSummary[],
  category: PdfImportCategory,
  offset = 0,
): PdfImportFileSummary[] {
  const candidates = files.filter((file) => file.category === category);
  if (candidates.length <= 6) return candidates;
  const byFolder = new Map<string, PdfImportFileSummary[]>();
  for (const file of candidates) {
    const slash = file.logical_path.lastIndexOf('/');
    const folder = slash < 0 ? '' : file.logical_path.slice(0, slash);
    const group = byFolder.get(folder) ?? [];
    group.push(file);
    byFolder.set(folder, group);
  }
  const interleaved: PdfImportFileSummary[] = [];
  const groups = [...byFolder.values()];
  for (let row = 0; interleaved.length < candidates.length; row += 1) {
    for (const group of groups) {
      const item = group[row];
      if (item) interleaved.push(item);
    }
  }
  return Array.from({ length: 6 }, (_, index) => interleaved[(offset + index) % interleaved.length]);
}

export function ocrPolicyCount(snapshot: PdfImportSnapshot, policy: PdfImportOcrPolicy): number {
  const counts = snapshot.choices.destination.kind === 'existing'
    && snapshot.match.state === 'ready'
    && snapshot.match.new_category_counts
    ? snapshot.match.new_category_counts
    : snapshot.category_counts;
  if (policy === 'none') return 0;
  if (policy === 'all') {
    return (counts.text ?? 0)
      + (counts.suspicious ?? 0)
      + (counts.missing ?? 0)
      + (counts.image ?? 0);
  }
  if (policy === 'missing') return (counts.missing ?? 0) + (counts.image ?? 0);
  return (counts.suspicious ?? 0) + (counts.missing ?? 0) + (counts.image ?? 0);
}

export function canOpenCheckText(snapshot: PdfImportSnapshot): boolean {
  return snapshot.can_review_text;
}

export function destinationIsReady(snapshot: PdfImportSnapshot, destination: PdfImportDestination): boolean {
  if (destination.kind === 'new') return destination.sheet_name.trim().length > 0;
  return snapshot.match.state === 'ready' && snapshot.match.new_count > 0;
}

export function newerSnapshot(
  current: PdfImportSnapshot | null,
  next: PdfImportSnapshot,
): PdfImportSnapshot | null {
  if (current && current.draft_id !== next.draft_id) return current;
  if (current && next.revision < current.revision) return current;
  return next;
}

export function bytesLabel(value: number): string {
  return formatNumberDisplay(value, 'filesize') ?? `${value} B`;
}

export function count(value: number, singular: string, plural = `${singular}s`): string {
  return `${value.toLocaleString()} ${value === 1 ? singular : plural}`;
}
