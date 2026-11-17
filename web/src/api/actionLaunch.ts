import type {
  HttpActionResult,
  HttpActionRunRequest,
} from '../generated/openHttpContracts';
import { httpContract, type HttpContractSuccessResponse, type ContractErrorFactory } from './httpContract';

interface ActionLaunchOptions {
  projectId?: string;
  signal?: AbortSignal;
}

export type ActionLaunchRequest = HttpActionRunRequest;
export type ActionLaunchWire =
  HttpContractSuccessResponse<'tenant.v1_action_run.post'>;

interface ActionLaunchResponse {
  status: number;
  result: ActionLaunchWire;
}

class ActionLaunchTransportError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super(`action launch transport response ${status}`);
    this.name = 'ActionLaunchTransportError';
    this.status = status;
    this.payload = payload;
  }
}

/**
 * The execution route deliberately carries an ActionResult on selected
 * non-2xx statuses. This is a discriminant branch, not a response validator:
 * it preserves the server's confirmation/operation-result semantics while
 * ordinary hosted error envelopes remain ordinary ApiErrors.
 */
function isActionResult(payload: unknown): payload is HttpActionResult {
  return typeof payload === 'object'
    && payload !== null
    && (payload as { schema_version?: unknown }).schema_version === 'frisket.action_result.v1';
}

export function createActionLaunchApi(
  errorFactory: ContractErrorFactory,
  defaultProjectId: string,
): { launch(action: ActionLaunchRequest, options?: ActionLaunchOptions): Promise<ActionLaunchResponse> } {
  return {
    async launch(action, options = {}) {
      try {
        const result = await httpContract(
          'tenant.v1_action_run.post',
          {
            pathParams: { pid: options.projectId ?? defaultProjectId },
            body: action,
            signal: options.signal,
            errorFactory: (status, payload) => new ActionLaunchTransportError(status, payload),
          },
        );
        return { status: 200, result };
      } catch (error) {
        if (error instanceof ActionLaunchTransportError && isActionResult(error.payload)) {
          return { status: error.status, result: error.payload };
        }
        if (error instanceof ActionLaunchTransportError) {
          throw errorFactory(error.status, error.payload);
        }
        throw error;
      }
    },
  };
}
