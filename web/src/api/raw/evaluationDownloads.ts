import type { EvaluationRequest } from '../evaluation';

/** Attachment responses need bytes, not the JSON transport's response decoder. */
export async function downloadEvaluation(
  projectId: string,
  resource: 'download.csv' | 'settings.json',
  request: EvaluationRequest,
  errorFactory: (status: number, payload: unknown) => Error,
): Promise<Blob> {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/evaluation/${resource}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
  if (!response.ok) throw errorFactory(response.status, await response.json().catch(() => ({})));
  return response.blob();
}
