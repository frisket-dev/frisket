import { httpContract } from './httpContract';

export interface DiagnosticsOptions {
  fetch?: typeof globalThis.fetch;
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface DiagnosticsReport {
  healthy: boolean;
  core: unknown;
  info: unknown;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
export function fetchDiagnostics(
  projectId: string | undefined,
  options: DiagnosticsOptions = {},
): Promise<DiagnosticsReport> {
  const errorFactory: ContractErrorFactory = (status) => new Error(`diagnose returned ${status}`);
  if (projectId === undefined) {
    return httpContract(
      'tenant.diagnose.get',
      {
        pathParams: {},
        query: {},
        fetch: options.fetch,
        signal: options.signal,
        headers: options.headers,
        errorFactory,
      },
    );
  }
  return httpContract(
    'tenant.project_diagnose.get',
    {
      pathParams: { pid: projectId },
      query: {},
      fetch: options.fetch,
      signal: options.signal,
      headers: options.headers,
      errorFactory,
    },
  );
}
