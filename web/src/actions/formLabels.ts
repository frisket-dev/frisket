import type { ActionParam } from '../api/types';

/** Labels shared by custom action forms and their exported presentation. */
export const FORM_LABELS = {
  outputColumns: 'Columns it creates',
  outputDetails: 'Output details',
  citations: 'Citations',
  citationSources: 'Citation sources',
  document: 'Document',
  pdfDpi: 'PDF render resolution (DPI)',
  searchablePdf: 'Create searchable PDF',
  keepTextPosition: 'Keep text position',
  media: 'Media',
  language: 'Language',
  expectedLanguages: 'Expected languages',
  identifySpeakers: 'Identify speakers',
  speakerCount: 'Speaker count',
  exactSpeakers: 'Exact speaker count',
  minSpeakers: 'Minimum speakers',
  maxSpeakers: 'Maximum speakers',
  modelSize: 'Model size',
  vad: 'Voice Activity Detection',
} as const;

type PresentedParam = { name: string; label: string; availability?: string; alternateLabel?: string;
  advanced?: boolean; choiceOptions?: { value: string; label: string }[] };

/** Custom bodies combine or conditionally render canonical fields. Return the
 * controls actually presented, retaining catalog details where they apply. */
export function customFormParams(kind: string, params: readonly ActionParam[]): PresentedParam[] | null {
  const byName = new Map(params.map((param) => [param.name, param]));
  const field = (name: string, label?: string, extra?: Partial<PresentedParam>): PresentedParam => {
    const param = byName.get(name);
    if (!param) throw new Error(`Missing ${kind} form parameter ${name}`);
    return { name, label: label ?? param.label, ...extra };
  };
  switch (kind) {
    case 'map.extract':
      return [field('source'), field('engine'), field('fields', FORM_LABELS.outputColumns),
        field('instruction'), field('include_confidence'),
        { name: 'citation_mode', label: FORM_LABELS.citations, choiceOptions: [
            { value: 'none', label: 'Off' }, { value: 'cite', label: 'Cite sources' },
            { value: 'require', label: 'Require citations' },
          ], availability: 'Citation options depend on the input type.' },
        field('source_document_columns', FORM_LABELS.citationSources,
          { advanced: true, availability: 'Shown when citations are enabled.' }),
        field('context', undefined, { advanced: true })];
    case 'media.ocr':
      return [field('source', FORM_LABELS.document), field('engine'),
        field('language', FORM_LABELS.language, { alternateLabel: FORM_LABELS.expectedLanguages,
          availability: 'The selected engine determines whether and how language is shown.' }),
        field('dpi', FORM_LABELS.pdfDpi), field('keep_text_position', FORM_LABELS.keepTextPosition,
          { availability: 'Shown when the selected engine supports text positions.' }),
        field('searchable_pdf', FORM_LABELS.searchablePdf)];
    case 'media.transcribe':
      return [field('source', FORM_LABELS.media), field('engine'),
        field('language', FORM_LABELS.language, { alternateLabel: FORM_LABELS.expectedLanguages,
          availability: 'The selected engine determines whether and how language is shown.' }),
        field('diarize', FORM_LABELS.identifySpeakers, { availability: 'Shown for engines that support speaker identification.' }),
        field('num_speakers', FORM_LABELS.exactSpeakers, { availability: 'Shown for supported speaker-count controls.' }),
        field('min_speakers', FORM_LABELS.minSpeakers, { availability: 'Shown for supported speaker-count ranges.' }),
        field('max_speakers', FORM_LABELS.maxSpeakers, { availability: 'Shown for supported speaker-count ranges.' }),
        field('model_size', FORM_LABELS.modelSize, { availability: 'Shown for engines with selectable model sizes.' }),
        field('vad', FORM_LABELS.vad, { availability: 'Shown for engines that support voice activity detection.' }),
        field('context', undefined, { availability: 'Shown when supported by the selected engine.' }),
        field('clean', undefined, { availability: 'Shown when supported by the selected engine.' })];
    default: return null;
  }
}
