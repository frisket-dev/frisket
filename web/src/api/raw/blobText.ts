/**
 * Blob resources are text streams rather than same-deploy JSON contracts.
 * Callers name the resource they need; they cannot choose transport details.
 */

/** Read a text evidence blob, rejecting so the evidence surface can use its quote fallback. */
export async function readEvidenceTextResource(url: string): Promise<string> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(String(response.status));
  return response.text();
}

/** Read a document text blob while preserving the caller-owned abort signal. */
export async function readDocumentTextResource(
  url: string,
  { signal }: { signal: AbortSignal },
): Promise<string> {
  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error(`Could not load this file (${response.status}).`);
  return response.text();
}
