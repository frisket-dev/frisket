import {
  httpContract,
  type HttpContractSuccessResponse,
} from '../httpContract';
import type {
  CopilotChatMessageInput,
  CopilotReply,
} from '../types';

type CopilotReplyWire = HttpContractSuccessResponse<'tenant.copilot_ep.post'>;

type ContractErrorFactory = (status: number, payload: unknown) => Error;

export interface CopilotContractOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

// Copilot chat → runnable proposals, plus the needsImport decision. Domain
// types live in ../types; this module owns the wire → domain mapping.
function mapCopilotReply(wire: CopilotReplyWire): CopilotReply {
  return {
    reply: wire.reply,
    needsImport: wire.needs_import,
    costUsd: wire.cost_usd ?? null,
    proposals: wire.proposals.map((proposal) => {
      const scope = proposal.spec.scope;
      return {
        kind: proposal.kind,
        title: proposal.title,
        spec: {
          action_id: proposal.spec.action_id,
          scope: scope.kind === 'project' ? { kind: 'project' as const } : {
            kind: 'sheet_rows' as const,
            sheet_id: scope.sheet_id,
            ...(scope.row_ids == null ? {} : { row_ids: [...scope.row_ids] }),
          },
          params: proposal.spec.params,
          output_names: proposal.spec.output_names ?? {},
          ...('sheet_name' in proposal.spec && typeof proposal.spec.sheet_name === 'string'
            ? { sheet_name: proposal.spec.sheet_name } : {}),
        },
      };
    }),
  };
}

export function copilotChatContract(
  projectId: string,
  messages: CopilotChatMessageInput[],
  errorFactory: ContractErrorFactory,
  model?: string | null,
  options: CopilotContractOptions = {},
): Promise<CopilotReply> {
  return httpContract('tenant.copilot_ep.post', {
    pathParams: { pid: projectId },
    query: {},
    // model omitted (not null) when unset: the wire contract's `model` is
    // optional 'provider/name' and the server owns the default.
    body: model ? { messages, model } : { messages },
    signal: options.signal,
    headers: options.headers,
    errorFactory,
  }, mapCopilotReply);
}
