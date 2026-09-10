export type ImportMode = 'csv' | 'paste' | 'xlsx' | 'files' | 'urls' | 'feed';
export type FileImportMode = Exclude<ImportMode, 'paste' | 'urls' | 'feed'>;
export type ImportWorkspaceStage = 'detect' | 'map' | 'confirm';

export interface ImportModeOption {
  id: ImportMode;
  label: string;
  hint: string;
}

export interface ImportWorkspaceStageOption {
  id: ImportWorkspaceStage;
  label: string;
  testId: string;
}

export const IMPORT_MODES: ImportModeOption[] = [
  { id: 'csv', label: 'One file', hint: 'CSV or Excel' },
  { id: 'files', label: 'Multiple files', hint: 'PDFs, images, audio' },
  { id: 'feed', label: 'Feed or source', hint: 'RSS, YouTube, API' },
];

export const IMPORT_WORKSPACE_STAGES: ImportWorkspaceStageOption[] = [
  { id: 'detect', label: 'Import', testId: 'import-workspace-stage-detect' },
  { id: 'map', label: 'Check', testId: 'import-workspace-stage-map' },
  { id: 'confirm', label: 'Confirm', testId: 'import-workspace-stage-confirm' },
];

export const IMPORT_COLUMN_TYPES = [
  'text',
  'integer',
  'number',
  'boolean',
  'date',
  'link',
  'json',
];
