import type { HttpActionEstimateValidationRequest } from '../generated/openHttpContracts';
import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import type {
  ActionExecutionRequest,
  ActionParamResolution,
  ParamValidationResult,
  RegisteredActionRequest,
  RunEstimate,
} from './types';
import { isDeriveCompositeRequest } from './types';
import { resolveRunActionInvocation } from './v1ActionSession';

export interface ActionEstimateValidationOptions {
  projectId?: string;
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export type ActionEstimateValidationAction =
  HttpActionEstimateValidationRequest['action'];
export type ActionEstimateWire =
  HttpContractSuccessResponse<'tenant.action_v1_estimate.post'>;
export type ActionParamValidationWire =
  HttpContractSuccessResponse<'tenant.action_v1_validate_params.post'>;

export interface ActionEstimateValidationApi {
  estimate(
    action: ActionEstimateValidationAction,
    options?: ActionEstimateValidationOptions,
  ): Promise<ActionEstimateWire>;
  validateParams(
    action: ActionEstimateValidationAction,
    options?: ActionEstimateValidationOptions,
  ): Promise<ActionParamValidationWire>;
}

export interface ActionEstimateValidationDomainDependencies {
  errorFactory: ContractErrorFactory;
  projectId: string;
}

export interface ActionEstimateValidationDomainApi {
  estimate(request: ActionExecutionRequest): Promise<RunEstimate>;
  resolveParams(
    request: Pick<RegisteredActionRequest, 'action_id' | 'scope' | 'params'>,
  ): Promise<ActionParamResolution>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;

export function paramValidationFromV1Wire(
  diagnostics: ActionParamValidationWire['diagnostics'],
): ParamValidationResult {
  return Object.fromEntries(
    Object.entries(diagnostics).map(([name, diagnostic]) => [
      name,
      {
        ok: diagnostic.ok,
        ...(diagnostic.message == null ? {} : { message: diagnostic.message }),
        ...(diagnostic.position == null ? {} : { position: diagnostic.position }),
      },
    ]),
  );
}

/** THE v1-estimate projection: the wire payload to the client's RunEstimate.
 *
 * A WHITELIST, deliberately — the client renders only fields it has decided
 * how to render — which is also its hazard: a field this function does not
 * name is dropped before any surface can see it, silently. That is how the
 * action panel ended up quoting the provider's cost while the 402 modal
 * quoted the billed one. Exported so the whitelist is testable on its own,
 * without standing up the action catalog the request path needs. */
export function runEstimateFromV1Wire(
  estimate: ActionEstimateWire['estimate'],
): RunEstimate {
  return {
    rows: estimate.rows,
    cost: estimate.cost,
    pricing_key: estimate.pricing_key ?? undefined,
    engine: estimate.engine ?? undefined,
    remote_capability: estimate.remote_capability ?? undefined,
    requires_confirmation: estimate.requires_confirmation ?? undefined,
    avg_input_tokens: estimate.avg_input_tokens ?? undefined,
    // Not every priced run is priced per ROW. Transcription is quoted per
    // second of audio, and dropping the quantity here is what left the panel
    // with a bare number it could not put into a sentence.
    audio_seconds: estimate.audio_seconds ?? undefined,
    billing_label: estimate.billing_label ?? undefined,
    venue_label: estimate.venue_label ?? undefined,
    cost_source: estimate.cost_source,
    warning: estimate.warning ?? undefined,
    claims: estimate.claims ?? undefined,
    promise_set_hash: estimate.promise_set_hash ?? undefined,
    // Carried, not dropped: the panel renders through the same three-state
    // selector the cost gate does (actions/quotedCost), and BOTH fields are
    // load-bearing there — together they distinguish an explicit
    // Unpriceable verdict from an incomplete/unrated envelope.
    billed_cost: estimate.billed_cost,
    policy_id: estimate.policy_id,
  };
}

export function createActionEstimateValidationApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): ActionEstimateValidationApi {
  return {
    estimate(action, options = {}) {
      return httpContract(
        'tenant.action_v1_estimate.post',
        {
          pathParams: { pid: options.projectId ?? projectId },
          query: {},
          body: { action },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    validateParams(action, options = {}) {
      return httpContract(
        'tenant.action_v1_validate_params.post',
        {
          pathParams: { pid: options.projectId ?? projectId },
          query: {},
          body: { action },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}

/** RealApi owns the shared action session; this domain owns the captured
 * invocation, estimate/validation transport, and wire-to-public mapping. */
export function createActionEstimateValidationDomainApi({
  errorFactory,
  projectId,
}: ActionEstimateValidationDomainDependencies): ActionEstimateValidationDomainApi {
  const actionEstimateValidationApi = createActionEstimateValidationApi(errorFactory, projectId);

  return {
    async estimate(request) {
      if (isDeriveCompositeRequest(request)) request = request.extraction;
      const invocation = resolveRunActionInvocation(undefined, projectId);
      return runEstimateFromV1Wire((await actionEstimateValidationApi.estimate(
        request as unknown as ActionEstimateValidationAction,
        invocation,
      )).estimate);
    },

    async resolveParams(request) {
      const wire = await actionEstimateValidationApi.validateParams({
        action_id: request.action_id,
        scope: request.scope,
        params: request.params,
      });
      const outputs = wire.logical_outputs;
      return {
        diagnostics: paramValidationFromV1Wire(wire.diagnostics ?? {}),
        ...(typeof wire.creates_sheet === 'boolean' ? { creates_sheet: wire.creates_sheet } : {}),
        logical_outputs: (outputs ?? []).map((output) => ({
          key: output.key,
          column_type: output.column_type,
          ...(output.existing_column_policy == null ? {} : {
            existing_column_policy: output.existing_column_policy,
          }),
        })),
      };
    },
  };
}
