import { listTableFromNamedResult, namedResultForListTable } from '../actions/listTable';
import { canonicalJson } from '../actions/canonicalJson';
import { ApiError, firstNonEmptyString, v1ErrorMessage } from './contractErrors';
import {
  resolveRunActionInvocation,
  throwIfActionAborted,
  type ResolvedRunActionInvocation,
  type V1ActionResult,
  type V1ActionSession,
} from './v1ActionSession';
import {
  actionExecutionName,
  actionExecutionPrimaryOutput,
  actionExecutionRowIds,
  actionExecutionSheetId,
  isRegisteredActionRequest,
  type DeriveCompositeRequest,
  type ActionExecutionRequest,
  type BackfillResult,
  type CopilotProposal,
  type CopilotRegisteredActionDraft,
  type ProjectInvocationOptions,
  type RunActionInvocationOptions,
  type RunActionLaunchResult,
  type RunProgress,
  type RegisteredActionRequest,
} from './types';

interface RunContext {
  actionName: string;
  sheetId: string;
  targetColumnId: string;
  /** Explicit row scope (spec.row_ids) when the run targets selected rows;
   * null means the whole sheet. */
  rowIds: string[] | null;
}

export interface ColumnRunInfo {
  actionName: string;
  prompt: string;
  model: string;
}

export interface ActionRunsDependencies {
  v1ActionSession: V1ActionSession;
  runAction(
    req: ActionExecutionRequest,
    options?: RunActionInvocationOptions,
  ): Promise<RunActionLaunchResult>;
  getRunProgress(
    projectId: string,
    runId: number,
    options: { signal?: AbortSignal },
  ): Promise<RunProgress>;
  cancelRun(
    projectId: string,
    runId: number,
    options: { signal?: AbortSignal },
  ): Promise<unknown>;
  waitForV1ActionCompletion(
    result: V1ActionResult,
    fallbackMessage: string,
    invocation: ResolvedRunActionInvocation,
  ): Promise<V1ActionResult>;
  actionDisplayNameFromActionKind(actionKind: string): string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function firstOutputSheetId(outputs: V1ActionResult['outputs']): string | null {
  const output = outputs?.find((candidate) => (
    candidate.kind === 'sheet' && candidate.sheet_id != null
  ));
  return output?.sheet_id == null ? null : String(output.sheet_id);
}

export class ActionRunsApi {
  /** Context for runs started this session, so status reads can fill the UI
   * fields the backend endpoint does not carry. */
  private runMeta = new Map<string, RunContext>();
  /** Last run spec by project/sheet/column for AI column drawers. */
  private colRunInfo = new Map<string, ColumnRunInfo>();
  /** One execution key per in-memory proposal, stable across a 402 retry. */
  private proposalIdempotencyKeys = new WeakMap<CopilotProposal, string>();
  private readonly dependencies: ActionRunsDependencies;
  private readonly defaultProjectId: string;

  constructor(dependencies: ActionRunsDependencies, defaultProjectId: string) {
    this.dependencies = dependencies;
    this.defaultProjectId = defaultProjectId;
  }

  private get v1ActionSession(): V1ActionSession {
    return this.dependencies.v1ActionSession;
  }

  private runMetaKey(projectId: string, runId: string): string {
    return JSON.stringify([projectId, runId]);
  }

  private columnRunNameKey(projectId: string, sheetId: string, name: string): string {
    return JSON.stringify([projectId, 'sheet', sheetId, name]);
  }

  private columnRunIdKey(projectId: string, columnId: string): string {
    return JSON.stringify([projectId, 'column', columnId]);
  }

  columnRunInfo(
    projectId: string,
    sheetId: string,
    column: { id: string; name: string },
  ): ColumnRunInfo | undefined {
    return this.colRunInfo.get(this.columnRunNameKey(projectId, sheetId, column.name))
      ?? this.colRunInfo.get(this.columnRunIdKey(projectId, column.id));
  }

  rememberColumnRunInfo(projectId: string, columnId: string, info: ColumnRunInfo): void {
    this.colRunInfo.set(this.columnRunIdKey(projectId, columnId), info);
  }

  private registeredProposalRequest(
    proposal: CopilotProposal,
    spec: CopilotRegisteredActionDraft,
    confirmation?: string,
  ): RegisteredActionRequest {
    const idempotencyKey = this.proposalIdempotencyKeys.get(proposal)
      ?? `copilot-${spec.action_id}:${globalThis.crypto.randomUUID()}`;
    this.proposalIdempotencyKeys.set(proposal, idempotencyKey);
    return {
      action_id: spec.action_id,
      scope: spec.scope,
      params: spec.params,
      output_names: spec.output_names,
      ...(spec.sheet_name === undefined ? {} : { sheet_name: spec.sheet_name }),
      idempotency_key: idempotencyKey,
      ...(confirmation ? { confirmation } : {}),
    };
  }

  /** Run the exact Copilot envelope through the current catalog execution owner. */
  async runProposal(
    proposal: CopilotProposal,
    confirmed = false,
    consentedPromiseSetHash?: string,
    options?: RunActionInvocationOptions,
  ): Promise<{ run_id: number | null; output_sheet_id?: string | null }> {
    const invocation = resolveRunActionInvocation(options, this.defaultProjectId);
    throwIfActionAborted(invocation.signal);
    const request = this.registeredProposalRequest(
      proposal,
      proposal.spec,
      confirmed ? consentedPromiseSetHash : undefined,
    );
    const { runId, outputSheetId } = await this.dependencies.runAction(request, invocation);
    throwIfActionAborted(invocation.signal);
    if (runId === null && outputSheetId) return { run_id: null, output_sheet_id: outputSheetId };
    if (runId === null) throw new ApiError(500, 'Proposal action completed without a run id');
    return { run_id: Number(runId) };
  }

  async runAction(
    req: ActionExecutionRequest,
    options?: RunActionInvocationOptions,
  ): Promise<RunActionLaunchResult> {
    const invocation = resolveRunActionInvocation(options, this.defaultProjectId);
    throwIfActionAborted(invocation.signal);
    if (isRegisteredActionRequest(req)) {
      return this.runV1ActionSpec(req, req, invocation);
    }
    return this.runDerive(req, invocation);
  }

  private rememberRunActionContext(
    projectId: string,
    runId: string,
    req: ActionExecutionRequest,
    signal?: AbortSignal,
  ): void {
    throwIfActionAborted(signal);
    const actionName = actionExecutionName(req);
    this.runMeta.set(this.runMetaKey(projectId, runId), {
      actionName,
      sheetId: actionExecutionSheetId(req),
      targetColumnId: actionExecutionPrimaryOutput(req),
      rowIds: actionExecutionRowIds(req) ?? null,
    });
  }

  private async runV1ActionSpec(
    req: ActionExecutionRequest,
    spec: Record<string, unknown>,
    invocation: ResolvedRunActionInvocation,
  ): Promise<RunActionLaunchResult> {
    throwIfActionAborted(invocation.signal);
    let out: V1ActionResult;
    try {
      out = await this.v1ActionSession.postV1ActionSpec(spec, invocation);
    } catch (error) {
      if (error instanceof ApiError && !invocation.signal?.aborted) {
        this.v1ActionSession.clearV1ActionIdempotencyKey(spec, invocation.projectId);
      }
      throwIfActionAborted(invocation.signal);
      throw error;
    }
    throwIfActionAborted(invocation.signal);
    const jobId = out.job_id ?? null;
    const receiptId = out.receipt_id ?? null;
    if (out.run_id == null) {
      if (
        out.status === 'completed'
        || out.status === 'partial'
        || out.status === 'queued'
        || ((out.outputs?.length ?? 0) > 0 && out.receipt_id != null)
      ) {
        throwIfActionAborted(invocation.signal);
        this.v1ActionSession.clearV1ActionIdempotencyKey(spec, invocation.projectId);
        return {
          runId: null,
          jobId,
          receiptId,
          status: out.status,
          outputSheetId: firstOutputSheetId(out.outputs),
        };
      }
      const message = firstNonEmptyString(
        out.errors.find((error) => error.message)?.message,
        out.errors[0]?.message,
        'Action did not return a run id',
      );
      throw new ApiError(500, message, out.errors[0]?.code);
    }
    throwIfActionAborted(invocation.signal);
    this.v1ActionSession.clearV1ActionIdempotencyKey(spec, invocation.projectId);
    const runId = String(out.run_id);
    this.rememberRunActionContext(invocation.projectId, runId, req, invocation.signal);
    return { runId, jobId, receiptId };
  }

  /** Compose map.extract + derive.table_from_list for the one-click
   * list-to-child-sheet command. */
  private async runDerive(
    req: DeriveCompositeRequest,
    invocation: ResolvedRunActionInvocation,
  ): Promise<RunActionLaunchResult> {
    throwIfActionAborted(invocation.signal);
    const itemField = req.itemField;
    const extraction = req.extraction;
    const fields = extraction.params.fields;
    const lists = Array.isArray(fields)
      ? fields.filter((field) => isRecord(field) && field.type === 'list')
      : [];
    if (!req.sheet_name.trim() || lists.length !== 1
      || !isRecord(lists[0]) || lists[0].name !== itemField) {
      throw new ApiError(400, 'Choose one list field and enter a name for the new sheet.');
    }
    // The ordinary run host confirms the composite. Its exact promise and
    // replacement intent belong to the paid extraction step on retry.
    const extractSpec = {
      ...extraction,
      ...(req.confirmation ? { confirmation: req.confirmation } : {}),
      ...(req.replace_existing ? { replace_existing: true } : {}),
    };
    if (extractSpec.action_id !== 'map.extract'
      || extractSpec.scope.kind !== 'sheet_rows'
    ) {
      throw new ApiError(400, 'Derive requires a canonical row extraction request.');
    }
    const extractResult = await this.dependencies.waitForV1ActionCompletion(
      await this.v1ActionSession.postV1ActionSpec(extractSpec, invocation),
      'map.extract did not complete before derive.table_from_list',
      invocation,
    );
    throwIfActionAborted(invocation.signal);
    const namedResult = namedResultForListTable(extractResult.outputs, itemField);
    throwIfActionAborted(invocation.signal);
    const draft = listTableFromNamedResult(req, itemField, namedResult);
    // A retry remains the same two-step operation, including after success or
    // a new browser API session. A changed destination/schema is a new intent.
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(
      canonicalJson({ extraction_key: extraction.idempotency_key, action: draft }),
    ));
    throwIfActionAborted(invocation.signal);
    const deriveSpec: RegisteredActionRequest = {
      ...draft,
      idempotency_key: `derive-${Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')}`,
    };
    throwIfActionAborted(invocation.signal);
    const deriveResult = await this.v1ActionSession.postV1ActionSpec(deriveSpec, invocation);
    throwIfActionAborted(invocation.signal);
    if (deriveResult.status !== 'completed') {
      throw new ApiError(
        500,
        v1ErrorMessage(deriveResult, 'derive.table_from_list did not complete'),
      );
    }
    throwIfActionAborted(invocation.signal);
    const runId = String(extractResult.run_id);
    const actionName = actionExecutionName(req);
    throwIfActionAborted(invocation.signal);
    this.runMeta.set(this.runMetaKey(invocation.projectId, runId), {
      actionName,
      sheetId: actionExecutionSheetId(req),
      targetColumnId: actionExecutionPrimaryOutput(req),
      rowIds: actionExecutionRowIds(req) ?? null,
    });
    throwIfActionAborted(invocation.signal);
    this.colRunInfo.set(this.columnRunNameKey(invocation.projectId, actionExecutionSheetId(req), actionExecutionPrimaryOutput(req)), {
      actionName,
      prompt: String(extraction.params.instruction ?? ''),
      model: String(extraction.params.model ?? ''),
    });
    return { runId, status: deriveResult.status,
      receiptId: deriveResult.receipt_id ?? null,
      outputSheetId: firstOutputSheetId(deriveResult.outputs) };
  }

  private async getRunProgressForInvocation(
    runId: string,
    invocation: ResolvedRunActionInvocation,
  ): Promise<RunProgress> {
    throwIfActionAborted(invocation.signal);
    const progress = await this.dependencies.getRunProgress(
      invocation.projectId,
      Number(runId),
      { signal: invocation.signal },
    );
    throwIfActionAborted(invocation.signal);
    const context = this.runMeta.get(this.runMetaKey(invocation.projectId, runId));
    return {
      ...progress,
      runId: progress.runId || runId,
      actionName: progress.actionName
        || context?.actionName
        || this.dependencies.actionDisplayNameFromActionKind(progress.actionKind),
      sheetId: progress.sheetId || context?.sheetId || '',
      targetColumnId: context?.targetColumnId ?? '',
      targetRowIds: context?.rowIds ?? null,
    };
  }

  getRunProgress(runId: string, options?: ProjectInvocationOptions): Promise<RunProgress> {
    return this.getRunProgressForInvocation(
      runId,
      resolveRunActionInvocation(options, this.defaultProjectId),
    );
  }

  async cancelRun(
    runId: string,
    options?: ProjectInvocationOptions,
  ): Promise<RunProgress> {
    const invocation = resolveRunActionInvocation(options, this.defaultProjectId);
    throwIfActionAborted(invocation.signal);
    await this.dependencies.cancelRun(
      invocation.projectId,
      Number(runId),
      { signal: invocation.signal },
    );
    return this.getRunProgressForInvocation(runId, invocation);
  }

  async backfillColumn(
    sheetId: string,
    columnName: string,
    confirmed = false,
    rowIds?: number[],
    consentedPromiseSetHash?: string,
  ): Promise<BackfillResult> {
    const spec = this.v1ActionSession.registeredActionSpec({
      action_id: 'run.backfill',
      scope: {
        kind: 'sheet_rows',
        sheet_id: Number(sheetId),
        ...(rowIds === undefined ? {} : { row_ids: rowIds }),
      },
      params: { column: columnName },
      output_names: {},
    });
    if (confirmed && consentedPromiseSetHash) spec.confirmation = consentedPromiseSetHash;
    return this.v1ActionSession.withV1ActionResult(spec, (result) => {
      this.v1ActionSession.assertCompletedV1ActionResult(
        result,
        'run.backfill did not complete',
        500,
      );
      const output = (result.outputs ?? []).find(
        (candidate) => candidate.kind === 'run_backfill',
      );
      const filled = typeof output?.ref?.filled === 'number' ? output.ref.filled : 0;
      const runId = typeof output?.ref?.run_id === 'number'
        ? output.ref.run_id
        : result.run_id;
      if (typeof runId !== 'number') {
        throw new ApiError(500, 'run.backfill did not return a run id');
      }
      return { filled, runId: String(runId) };
    }, { clearIdempotency: 'afterPost' });
  }
}

export function createActionRunsApi(
  dependencies: ActionRunsDependencies,
  defaultProjectId: string,
): ActionRunsApi {
  return new ActionRunsApi(dependencies, defaultProjectId);
}
