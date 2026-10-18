export type TableImportStage = 'add' | 'rows' | 'columns' | 'import';

export const TABLE_IMPORT_STAGES = [
  { id: 'add', label: 'Add data', testId: 'table-import-stage-add' },
  { id: 'rows', label: 'Check rows', testId: 'table-import-stage-rows' },
  { id: 'columns', label: 'Columns', testId: 'table-import-stage-columns' },
  { id: 'import', label: 'Import', testId: 'table-import-stage-import' },
] as const;

export type TableImportSourceMode = 'file' | 'paste';

export type TableImportSourceIssue =
  | { kind: 'multiple'; files: File[] }
  | { kind: 'pdf'; file: File }
  | { kind: 'follow_the_money'; file: File }
  | { kind: 'unsupported'; filename: string; message?: string }
  | { kind: 'empty'; filename: string }
  | { kind: 'large_paste'; size: number };

function extensionOf(filename: string): string {
  const match = /\.([^.]+)$/.exec(filename.trim().toLocaleLowerCase());
  return match?.[1] ?? '';
}

export function sourceIssueForFiles(files: File[], followTheMoneyEnabled = true): TableImportSourceIssue | null {
  if (files.length > 1) return { kind: 'multiple', files };
  const file = files[0];
  if (!file) return null;
  const extension = extensionOf(file.name);
  if (extension === 'pdf' || file.type === 'application/pdf') return { kind: 'pdf', file };
  if ((extension === 'json' || extension === 'jsonl') && followTheMoneyEnabled) return { kind: 'follow_the_money', file };
  if (file.size === 0) return { kind: 'empty', filename: file.name };
  // The server identifies table formats from their bytes. Unknown and
  // extensionless files must reach that inspection rather than being rejected
  // from their names alone.
  return null;
}

export interface PasteSummary {
  columns: number;
  delimiter: 'tabs' | 'commas' | 'one';
  lines: number;
}

/**
 * A deliberately cheap display-only summary. The staged server reader remains
 * authoritative for parsing, row boundaries, columns, and every import fact.
 */
export function summarizePasteForDisplay(value: string): PasteSummary {
  if (!value) return { columns: 0, delimiter: 'one', lines: 0 };
  let lines = 1;
  let first = '';
  let lineStart = 0;
  const sampleLimit = Math.min(value.length, 64 * 1024);
  for (let index = 0; index < value.length; index += 1) {
    const character = value.charCodeAt(index);
    if (character !== 10 && character !== 13) continue;
    if (!first && index <= sampleLimit && index > lineStart) first = value.slice(lineStart, index);
    if (character === 13 && value.charCodeAt(index + 1) === 10) index += 1;
    lines += 1;
    lineStart = index + 1;
  }
  if (value.endsWith('\n') || value.endsWith('\r')) lines -= 1;
  if (!first && lineStart < sampleLimit) first = value.slice(lineStart, sampleLimit);
  const tabs = (first.match(/\t/g) ?? []).length;
  const commas = (first.match(/,/g) ?? []).length;
  if (tabs > 0 && tabs >= commas) return { columns: tabs + 1, delimiter: 'tabs', lines };
  if (commas > 0) return { columns: commas + 1, delimiter: 'commas', lines };
  return { columns: first ? 1 : 0, delimiter: 'one', lines };
}

export function tableImportDefaultSheetName(filename: string, inputKind: 'file' | 'paste'): string {
  if (inputKind === 'paste') return 'Pasted rows';
  const withoutExtension = filename.replace(/\.[^.]+$/, '').trim();
  return withoutExtension || 'Imported table';
}
