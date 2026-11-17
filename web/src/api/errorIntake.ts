import { httpContract, type ContractErrorFactory, type HttpContractSuccessResponse } from "./httpContract";

interface ErrorIntakeOptions {
  signal?: AbortSignal;
  credentials?: RequestCredentials;
  keepalive?: boolean;
}

interface ClientErrorReportRequest {
  source: string;
  severity: string;
  name?: string | null;
  message: string;
  stack?: string | null;
  route?: string | null;
  context?: Record<string, unknown>;
}

interface ClientErrorReportResponse {
  ok: boolean;
  id: number;
  accepted?: boolean | null;
}

interface DiagnosticBundleRequest {
  message?: string | null;
  route?: string | null;
  context?: Record<string, unknown>;
  include_raw_values?: boolean;
  recent_client_error_ids?: number[];
}

interface DiagnosticBundleResponse {
  ok: boolean;
  report_id: number;
  bundle: Record<string, unknown>;
}

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

export function createErrorIntakeApi(errorFactory?: ContractErrorFactory) {
  return {
    reportClientError(
      body: ClientErrorReportRequest,
      options: ErrorIntakeOptions = {},
    ): Promise<ClientErrorReportResponse> {
      return httpContract(
        "outer.client_errors.post",
        {
          body: body as never,
          signal: options.signal,
          credentials: options.credentials,
          keepalive: options.keepalive,
          errorFactory,
        },
      );
    },
    createDiagnosticBundle(
      body: DiagnosticBundleRequest,
      options: ErrorIntakeOptions = {},
    ): Promise<DiagnosticBundleResponse> {
      return httpContract(
        "outer.diagnostic_bundle.post",
        {
          // The wire request is the public route's open sanitized dict; the
          // domain interface above is the shipped sender's actual shape.
          body: body as never,
          signal: options.signal,
          errorFactory,
        },
        mapDiagnosticBundle,
      );
    },
  };
}
