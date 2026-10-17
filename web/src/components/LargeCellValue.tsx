import { useCallback } from 'react';
import type { BoundedCellValue } from '../api/types';
import { getCellTextWindowContract } from '../api/httpContractRoutes/cells';
import { cellValueDownloadUrl, readFullCellValueResource } from '../api/raw/cellValue';
import { BoundedTextReader, type BoundedTextReaderProps } from './BoundedTextReader';

export function LargeCellValue({ projectId, rowId, columnId, descriptor }: {
  projectId: string; rowId: string; columnId: string; descriptor: BoundedCellValue;
}) {
  const loadWindow = useCallback<BoundedTextReaderProps['loadWindow']>(
    async (options) => {
      const window = await getCellTextWindowContract(projectId, rowId, columnId, options);
      return { ...window, previous_start: window.previous_start ?? null, next_start: window.next_start ?? null };
    },
    [projectId, rowId, columnId],
  );
  const downloadUrl = useCallback((revision: number) => cellValueDownloadUrl(projectId, rowId, columnId, revision), [projectId, rowId, columnId]);
  const copyFullValue = useCallback((revision: number, signal: AbortSignal) => readFullCellValueResource(downloadUrl(revision), signal), [downloadUrl]);
  return <BoundedTextReader key={`${projectId}:${rowId}:${columnId}:${descriptor.retrieval_revision}`}
    initialRevision={descriptor.retrieval_revision} totalCharacters={descriptor.total_characters} totalBytes={descriptor.total_bytes}
    loadWindow={loadWindow} downloadUrl={downloadUrl} copyFullValue={copyFullValue} />;
}
