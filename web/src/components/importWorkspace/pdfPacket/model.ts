import type {
  PdfPacketMatchResult,
  PdfPacketPhrase,
  PdfPacketSplitSnapshot,
} from '../../../api/pdfPacketSplits';

export type PdfPacketMethod = 'visual' | 'text' | 'manual';

export interface PdfPacketDocument {
  index: number;
  start: number;
  end: number;
  pages: number;
}

export interface PdfPacketFlowState {
  snapshot: PdfPacketSplitSnapshot | null;
  confirmedStarts: number[];
  fixedStarts: number[];
  examplePages: number[];
  rejected: number[];
  threshold: number;
  method: PdfPacketMethod;
  phrases: PdfPacketPhrase[];
  fuzzy: boolean;
  matches: PdfPacketMatchResult | null;
  matching: boolean;
  error: string | null;
  dirty: boolean;
}

export type PdfPacketFlowAction =
  | { type: 'prepared'; snapshot: PdfPacketSplitSnapshot }
  | { type: 'snapshot'; snapshot: PdfPacketSplitSnapshot }
  | { type: 'toggleStart'; page: number }
  | { type: 'toggleFileExamples'; enabled: boolean }
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
  | { type: 'error'; message: string | null };

export const initialPdfPacketFlowState: PdfPacketFlowState = {
  snapshot: null,
  confirmedStarts: [1],
  fixedStarts: [1],
  examplePages: [1],
  rejected: [],
  threshold: 80,
  method: 'visual',
  phrases: [],
  fuzzy: false,
  matches: null,
  matching: false,
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
    case 'prepared': {
      const extended = action.snapshot as PdfPacketSplitSnapshot & { fixed_starts?: number[]; example_pages?: number[] };
      const fixedStarts = extended.fixed_starts ?? [1];
      return {
        ...initialPdfPacketFlowState,
        snapshot: action.snapshot,
        confirmedStarts: fixedStarts,
        fixedStarts,
        examplePages: extended.example_pages ?? [1],
      };
    }
    case 'snapshot':
      return {
        ...state,
        snapshot: action.snapshot,
        matches: action.snapshot.analysis_revision !== state.snapshot?.analysis_revision
          || action.snapshot.text_source !== state.snapshot?.text_source
          ? null
          : state.matches,
      };
    case 'toggleFileExamples':
      return { ...state, examplePages: action.enabled ? sortedUnique([...state.examplePages, ...state.fixedStarts]) : state.examplePages.filter((page) => !state.fixedStarts.includes(page)), matches: null, dirty: true };
    case 'toggleStart': {
      if (state.fixedStarts.includes(action.page)) {
        if (state.fixedStarts.length === 1) return state;
        return { ...state, examplePages: state.examplePages.includes(action.page) ? state.examplePages.filter((page) => page !== action.page) : sortedUnique([...state.examplePages, action.page]), matches: null, dirty: true };
      }
      const present = state.confirmedStarts.includes(action.page);
      return {
        ...state,
        confirmedStarts: present
          ? state.confirmedStarts.filter((page) => page !== action.page)
          : sortedUnique([...state.confirmedStarts, action.page]),
        rejected: state.rejected.filter((page) => page !== action.page),
        matches: null,
        dirty: true,
      };
    }
    case 'confirmStart':
      if (state.fixedStarts.includes(action.page)) return state;
      return {
        ...state,
        confirmedStarts: sortedUnique([...state.confirmedStarts, action.page]),
        rejected: state.rejected.filter((page) => page !== action.page),
        matches: null,
        dirty: true,
      };
    case 'reject':
      return state.fixedStarts.includes(action.page) ? state : {
        ...state,
        confirmedStarts: state.confirmedStarts.filter((page) => page !== action.page),
        rejected: sortedUnique([...state.rejected, action.page]),
        matches: null,
        dirty: true,
      };
    case 'threshold':
      return { ...state, threshold: action.value, matches: null };
    case 'method':
      return { ...state, method: action.method, matches: null };
    case 'addPhrase': {
      const id = crypto.randomUUID();
      return {
        ...state,
        phrases: [...state.phrases, { id, text: '', enabled: true, fuzzy: state.fuzzy }],
        matches: null,
      };
    }
    case 'phrase':
      return {
        ...state,
        phrases: state.phrases.map((phrase) => phrase.id === action.id
          ? { ...phrase, ...action.patch }
          : phrase),
        matches: null,
      };
    case 'removePhrase':
      return {
        ...state,
        phrases: state.phrases.filter((phrase) => phrase.id !== action.id),
        matches: null,
      };
    case 'fuzzy':
      return {
        ...state,
        fuzzy: action.value,
        phrases: state.phrases.map((phrase) => ({ ...phrase, fuzzy: action.value })),
        matches: null,
      };
    case 'matching':
      return { ...state, matching: true, error: null };
    case 'matches':
      return { ...state, matching: false, matches: action.value };
    case 'error':
      return { ...state, error: action.message, matching: false };
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

export function resamplePages(pageCount: number, previous: readonly number[]): number[] {
  const previousSet = new Set(previous);
  const unseen = Array.from({ length: pageCount }, (_, index) => index + 1)
    .filter((page) => !previousSet.has(page));
  const seen = previous.filter((page) => page >= 1 && page <= pageCount);
  const result: number[] = [];
  while (result.length < Math.min(6, pageCount)) {
    const pool = unseen.length ? unseen : seen;
    const index = Math.floor(Math.random() * pool.length);
    result.push(pool.splice(index, 1)[0]);
  }
  return result.sort((left, right) => left - right);
}

export function matchForPage(matches: PdfPacketMatchResult | null, page: number) {
  return matches?.pages.find((candidate) => candidate.page === page) ?? null;
}
