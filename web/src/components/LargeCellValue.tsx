import { useCallback } from 'react';
import type { RowTextTarget } from '../state/detailStore';
import type { BoundedCellValue } from '../api/types';
import { getCellTextWindowContract } from '../api/httpContractRoutes/cells';
import { cellValueDownloadUrl, readFullCellValueResource } from '../api/raw/cellValue';
import { BoundedTextReader, type BoundedTextReaderProps } from './BoundedTextReader';

export function LargeCellValue({ projectId, rowId, columnId, descriptor, textTarget }: {
  projectId: string; rowId: string; columnId: string; descriptor?: BoundedCellValue; textTarget?: RowTextTarget;
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
  return <BoundedTextReader key={`${projectId}:${rowId}:${columnId}:${textTarget?.requestKey ?? descriptor?.retrieval_revision}`}
    initialRevision={textTarget?.retrievalRevision ?? descriptor?.retrieval_revision} initialRange={textTarget?.range}
    totalCharacters={descriptor?.total_characters ?? 0} totalBytes={descriptor?.total_bytes ?? 0}
    loadWindow={loadWindow} downloadUrl={downloadUrl} copyFullValue={copyFullValue} />;
}
