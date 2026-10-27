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
  { id: 'pdf_split', label: 'Split PDFs', hint: 'Into documents or pages' },
  { id: 'feed', label: 'Feed or source', hint: 'RSS, YouTube, API' },
];
export interface ImportHandlers {
  onImported(sheetId: number): void;
  onError(message: string): void;
  /** The "Download media?" prompt's Download
   *  button routes here — the same runActionFromSurface(kind, column) shape
   *  the caret menu uses, so the action drawer opens pre-bound to the
   *  detected URL column. */
  onLaunchDownload(launcherKind: string, columnName: string): void;
  /** The "Find the names in it?" prompt's primary button. Opens the map.ner
   *  drawer on the just-imported sheet; it never starts extraction. */
  onExtractEntities?(): void;
}
