import { httpContract, type ContractErrorFactory } from './httpContract';

interface DiagnosticsOptions {
  fetch?: typeof globalThis.fetch;
  signal?: AbortSignal;
}

export interface DiagnosticsReport {
  healthy: boolean;
  core: unknown;
  info: unknown;
}

export function fetchDiagnostics(
  projectId: string | undefined,
  options: DiagnosticsOptions = {},
): Promise<DiagnosticsReport> {
  const errorFactory: ContractErrorFactory = (status) => new Error(`diagnose returned ${status}`);
  if (projectId === undefined) {
    return httpContract(
      'tenant.diagnose.get',
      {
        fetch: options.fetch,
        signal: options.signal,
        errorFactory,
      },
    );
  }
  return httpContract(
    'tenant.project_diagnose.get',
    {
      pathParams: { pid: projectId },
      fetch: options.fetch,
      signal: options.signal,
      errorFactory,
    },
  );
}
