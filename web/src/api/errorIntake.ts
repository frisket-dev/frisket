import { httpContract, type HttpContractSuccessResponse } from "./httpContract";

export interface ErrorIntakeOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
  credentials?: RequestCredentials;
  keepalive?: boolean;
}

export interface ClientErrorReportRequest {
  source: string;
  severity: string;
  name?: string | null;
  message: string;
  stack?: string | null;
  route?: string | null;
  context?: Record<string, unknown>;
}

export interface ClientErrorReportResponse {
  ok: boolean;
  id: number;
  accepted?: boolean | null;
}

export interface DiagnosticBundleRequest {
  message?: string | null;
  route?: string | null;
  context?: Record<string, unknown>;
  include_raw_values?: boolean;
  recent_client_error_ids?: number[];
}

export interface DiagnosticBundleResponse {
  ok: boolean;
  report_id: number;
  bundle: Record<string, unknown>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
type DiagnosticBundleWire =
  HttpContractSuccessResponse<"outer.diagnostic_bundle.post">;

function mapDiagnosticBundle(
  wire: DiagnosticBundleWire,
): DiagnosticBundleResponse {
  // The bundle payload is deliberately edition-divergent open JSON; the
  // generated artifact is compile-time transport information, not a runtime
  // validator, so the domain cast keeps the old untyped-client behavior.
  return {
    ...wire,
    bundle: wire.bundle as DiagnosticBundleResponse["bundle"],
  };
}

export interface ErrorIntakeApi {
  reportClientError(
    body: ClientErrorReportRequest,
    options?: ErrorIntakeOptions,
  ): Promise<ClientErrorReportResponse>;
  createDiagnosticBundle(
    body: DiagnosticBundleRequest,
    options?: ErrorIntakeOptions,
  ): Promise<DiagnosticBundleResponse>;
}

export function createErrorIntakeApi(
  errorFactory: ContractErrorFactory,
): ErrorIntakeApi {
  return {
    reportClientError(body, options = {}) {
      return httpContract(
        "outer.client_errors.post",
        {
          pathParams: {},
          query: {},
          body: body as never,
          signal: options.signal,
          headers: options.headers,
          credentials: options.credentials,
          keepalive: options.keepalive,
          errorFactory,
        },
      );
    },
    createDiagnosticBundle(body, options = {}) {
      return httpContract(
        "outer.diagnostic_bundle.post",
        {
          pathParams: {},
          query: {},
          // The wire request is the public route's open sanitized dict; the
          // domain interface above is the shipped sender's actual shape.
          body: body as never,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        mapDiagnosticBundle,
      );
    },
  };
}
