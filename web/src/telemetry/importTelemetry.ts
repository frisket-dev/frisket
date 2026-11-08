import { byteBucket, columnBucket, durationBucket, failureCategory, rowBucket, sendProductTelemetry, type ProductTelemetryEvent } from './productTelemetry';

type Submission = Extract<ProductTelemetryEvent, { type: 'Import.submitted' }>['properties'];
type ImportFacts = Pick<Submission, 'importKind' | 'format'> & { bytes?: number; columns?: number };
type ImportOutcome = Pick<Submission, 'result'> & { rows?: number; columns?: number };

/** Observe one user submission; accepting a background import isn't completion. */
export async function trackImportSubmission<T>(
  projectId: string,
  facts: ImportFacts,
  request: () => Promise<T>,
  outcome: (response: T) => ImportOutcome = () => ({ result: 'queued' }),
): Promise<T> {
  const startedAt = performance.now();
  const common = { importKind: facts.importKind, format: facts.format, bytes: byteBucket(facts.bytes) };
  sendProductTelemetry({ type: 'Import.started', properties: common }, projectId);
  try {
    const response = await request();
    const result = outcome(response);
    sendProductTelemetry({ type: 'Import.submitted', properties: {
      ...common, rows: rowBucket(result.rows), columns: columnBucket(result.columns ?? facts.columns),
      requestDuration: durationBucket(performance.now() - startedAt), result: result.result,
      failureCategory: result.result === 'failed' || result.result === 'partial' ? 'unknown' : 'none',
    } }, projectId);
    return response;
  } catch (error) {
    const cancelled = error instanceof DOMException && error.name === 'AbortError';
    sendProductTelemetry({ type: 'Import.submitted', properties: {
      ...common, rows: 'unknown', columns: columnBucket(facts.columns),
      requestDuration: durationBucket(performance.now() - startedAt),
      result: cancelled ? 'cancelled' : 'failed', failureCategory: cancelled ? 'none' : failureCategory(error),
    } }, projectId);
    throw error;
  }
}
