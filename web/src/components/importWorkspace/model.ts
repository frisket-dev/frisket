export type ImportMode = 'csv' | 'paste' | 'xlsx' | 'files' | 'pdf_split' | 'urls' | 'feed';
export type FileImportMode = Exclude<ImportMode, 'paste' | 'pdf_split' | 'urls' | 'feed'>;
export interface ImportModeOption {
  id: ImportMode;
  label: string;
  hint: string;
}

export const IMPORT_MODES: ImportModeOption[] = [
  { id: 'csv', label: 'One file', hint: 'CSV or Excel' },
  { id: 'files', label: 'Multiple files', hint: 'PDFs, images, audio' },
  { id: 'pdf_split', label: 'Split a PDF', hint: 'One packet into many documents' },
  { id: 'feed', label: 'Feed or source', hint: 'RSS, YouTube, API' },
];
