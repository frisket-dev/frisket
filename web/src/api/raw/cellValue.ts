/** Browser-native exact cell download; excerpts never pass through this path. */
export function cellValueDownloadUrl(
  projectId: string, rowId: string, columnId: string, revision: number,
): string {
  return `/api/projects/${encodeURIComponent(projectId)}/cells/${encodeURIComponent(rowId)}/${encodeURIComponent(columnId)}/download?revision=${revision}`;
}

/** Only called after a deliberate full-copy action. Download stays streaming. */
export async function readFullCellValueResource(url: string, signal?: AbortSignal): Promise<string> {
  const response = await fetch(url, { signal });
  if (response.status === 409) throw new Error('This value changed. Reload the text before copying.');
  if (!response.ok) throw new Error(`Could not copy the full value (${response.status}).`);
  return response.text();
}
