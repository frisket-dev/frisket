import type {
  PdfPacketMatchResult,
  PdfPacketPage,
  PdfPacketPhrase,
  PdfPacketSplitSnapshot,
} from '../../../api/pdfPacketSplits';

export type PdfPacketStage = 'check' | 'find' | 'import';
export type PdfPacketMethod = 'visual' | 'text' | 'manual';

export const PDF_PACKET_STAGES = [
  { id: 'check', label: 'Check text', testId: 'pdf-packet-stage-check' },
  { id: 'find', label: 'Find documents', testId: 'pdf-packet-stage-find' },
  { id: 'import', label: 'Import', testId: 'pdf-packet-stage-import' },
] as const;

export interface PdfPacketDocument {
  index: number;
  start: number;
  end: number;
  pages: number;
}

export interface PdfPacketRememberedOptions {
  namePattern: string;
  keepOcrText: boolean;
}

export interface PdfPacketFlowState {
  snapshot: PdfPacketSplitSnapshot | null;
  stage: PdfPacketStage;
  selectedPage: number;
  pages: Record<number, PdfPacketPage>;
  pageLoading: number | null;
  confirmedStarts: number[];
  rejected: number[];
  threshold: number;
  method: PdfPacketMethod;
  phrases: PdfPacketPhrase[];
  fuzzy: boolean;
  matches: PdfPacketMatchResult | null;
  matching: boolean;
  ocrEngine: string;
  ocrBusy: boolean;
  destinationName: string;
  namePattern: string;
  keepOcrText: boolean;
  rememberOptions: boolean;
  committing: boolean;
  error: string | null;
  dirty: boolean;
}

export type PdfPacketFlowAction =
  | { type: 'reset' }
  | { type: 'prepared'; snapshot: PdfPacketSplitSnapshot; remembered?: PdfPacketRememberedOptions | null }
  | { type: 'snapshot'; snapshot: PdfPacketSplitSnapshot }
  | { type: 'stage'; stage: PdfPacketStage }
  | { type: 'selectPage'; page: number }
  | { type: 'pageLoading'; page: number }
  | { type: 'pageLoaded'; page: PdfPacketPage }
  | { type: 'toggleStart'; page: number }
  | { type: 'confirmStart'; page: number }
  | { type: 'reject'; page: number }
  | { type: 'threshold'; value: number }
  | { type: 'method'; method: PdfPacketMethod }
  | { type: 'addPhrase' }
  | { type: 'phrase'; id: string; patch: Partial<PdfPacketPhrase> }
  | { type: 'removePhrase'; id: string }
  | { type: 'fuzzy'; value: boolean }
  | { type: 'matching' }
  | { type: 'matches'; value: PdfPacketMatchResult }
  | { type: 'acceptAll' }
  | { type: 'ocrEngine'; engine: string }
  | { type: 'ocrBusy'; value: boolean }
  | { type: 'destinationName'; value: string }
  | { type: 'namePattern'; value: string }
  | { type: 'keepOcrText'; value: boolean }
  | { type: 'rememberOptions'; value: boolean }
  | { type: 'committing'; value: boolean }
  | { type: 'error'; message: string | null };

export const initialPdfPacketFlowState: PdfPacketFlowState = {
  snapshot: null,
  stage: 'check',
  selectedPage: 1,
  pages: {},
  pageLoading: null,
  confirmedStarts: [1],
  rejected: [],
  threshold: 80,
  method: 'visual',
  phrases: [],
  fuzzy: false,
  matches: null,
  matching: false,
  ocrEngine: 'rapidocr',
  ocrBusy: false,
  destinationName: '',
  namePattern: '{packet} · pp {start}–{end}',
  keepOcrText: true,
  rememberOptions: false,
  committing: false,
  error: null,
  dirty: false,
};

function sortedUnique(pages: readonly number[]): number[] {
  return [...new Set(pages)].sort((a, b) => a - b);
}

export function pdfPacketFlowReducer(
  state: PdfPacketFlowState,
  action: PdfPacketFlowAction,
): PdfPacketFlowState {
  switch (action.type) {
    case 'reset':
      return initialPdfPacketFlowState;
    case 'prepared': {
      const base = action.snapshot.packet.filename.replace(/\.pdf$/i, '');
      return {
        ...initialPdfPacketFlowState,
        snapshot: action.snapshot,
        destinationName: `${base} documents`,
        namePattern: action.remembered?.namePattern ?? initialPdfPacketFlowState.namePattern,
        keepOcrText: action.remembered?.keepOcrText ?? initialPdfPacketFlowState.keepOcrText,
        rememberOptions: Boolean(action.remembered),
      };
    }
    case 'snapshot':
      return { ...state, snapshot: action.snapshot };
    case 'stage':
      return { ...state, stage: action.stage, error: null };
    case 'selectPage':
      return { ...state, selectedPage: action.page };
    case 'pageLoading':
      return { ...state, pageLoading: action.page };
    case 'pageLoaded':
      return {
        ...state,
        pages: { ...state.pages, [action.page.page]: action.page },
        pageLoading: state.pageLoading === action.page.page ? null : state.pageLoading,
      };
    case 'toggleStart': {
      if (action.page === 1) return state;
      const present = state.confirmedStarts.includes(action.page);
      return {
        ...state,
        confirmedStarts: present
          ? state.confirmedStarts.filter((page) => page !== action.page)
          : sortedUnique([...state.confirmedStarts, action.page]),
        rejected: state.rejected.filter((page) => page !== action.page),
        dirty: true,
      };
    }
    case 'confirmStart':
      return {
        ...state,
        confirmedStarts: sortedUnique([...state.confirmedStarts, action.page]),
        rejected: state.rejected.filter((page) => page !== action.page),
        dirty: true,
      };
    case 'reject':
      return action.page === 1 ? state : {
        ...state,
        confirmedStarts: state.confirmedStarts.filter((page) => page !== action.page),
        rejected: sortedUnique([...state.rejected, action.page]),
        dirty: true,
      };
    case 'threshold':
      return { ...state, threshold: action.value, dirty: true };
    case 'method':
      return { ...state, method: action.method };
    case 'addPhrase': {
      const id = crypto.randomUUID();
      return {
        ...state,
        phrases: [...state.phrases, { id, text: '', enabled: true, fuzzy: state.fuzzy }],
        dirty: true,
      };
    }
    case 'phrase':
      return {
        ...state,
        phrases: state.phrases.map((phrase) => phrase.id === action.id
          ? { ...phrase, ...action.patch }
          : phrase),
        dirty: true,
      };
    case 'removePhrase':
      return { ...state, phrases: state.phrases.filter((phrase) => phrase.id !== action.id), dirty: true };
    case 'fuzzy':
      return {
        ...state,
        fuzzy: action.value,
        phrases: state.phrases.map((phrase) => ({ ...phrase, fuzzy: action.value })),
        dirty: true,
      };
    case 'matching':
      return { ...state, matching: true, error: null };
    case 'matches':
      return { ...state, matching: false, matches: action.value };
    case 'acceptAll':
      return {
        ...state,
        confirmedStarts: sortedUnique([
          ...state.confirmedStarts,
          ...(state.matches?.suggested_pages ?? []),
        ]),
        rejected: state.rejected.filter((page) => !state.matches?.suggested_pages.includes(page)),
        dirty: true,
      };
    case 'ocrEngine':
      return { ...state, ocrEngine: action.engine };
    case 'ocrBusy':
      return { ...state, ocrBusy: action.value };
    case 'destinationName':
      return { ...state, destinationName: action.value, dirty: true };
    case 'namePattern':
      return { ...state, namePattern: action.value, dirty: true };
    case 'keepOcrText':
      return { ...state, keepOcrText: action.value, dirty: true };
    case 'rememberOptions':
      return { ...state, rememberOptions: action.value, dirty: true };
    case 'committing':
      return { ...state, committing: action.value };
    case 'error':
      return { ...state, error: action.message, matching: false, ocrBusy: false, committing: false };
  }
}

export function confirmedDocuments(starts: readonly number[], pageCount: number): PdfPacketDocument[] {
  const ordered = sortedUnique(starts.filter((page) => page >= 1 && page <= pageCount));
  return ordered.map((start, index) => {
    const end = (ordered[index + 1] ?? pageCount + 1) - 1;
    return { index: index + 1, start, end, pages: end - start + 1 };
  });
}

export function samplePages(pageCount: number): number[] {
  if (pageCount <= 6) return Array.from({ length: pageCount }, (_, index) => index + 1);
  return sortedUnique(Array.from({ length: 6 }, (_, index) => (
    1 + Math.round(index * (pageCount - 1) / 5)
  )));
}

export function matchForPage(matches: PdfPacketMatchResult | null, page: number) {
  return matches?.pages.find((candidate) => candidate.page === page) ?? null;
}
