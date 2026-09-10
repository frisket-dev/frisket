import { canonicalJson } from '../actions/canonicalJson';
import {
  createActionLaunchApi,
  type ActionLaunchRequest,
  type ActionLaunchWire,
} from './actionLaunch';
import {
  ApiError,
  actionLaunchErrorFromContract,
  v1ErrorMessage,
} from './contractErrors';
import type {
  JsonValue,
  RegisteredActionDraft,
  RegisteredActionRequest,
  RunActionInvocationOptions,
  SheetRowScope,
} from './types';

export interface V1ActionSpecActionOutput {
  kind: string;
  name: string | null;
  sheet_id?: number | null;
  column_id?: number | null;
  row_ids?: number[];
  ref?: Record<string, unknown>;
}

export type V1ActionResult = Omit<
  ActionLaunchWire,
  'schema_version' | 'errors' | 'outputs'
> & {
  schema_version: 'frisket.action_result.v1';
  errors: NonNullable<ActionLaunchWire['errors']>;
  outputs?: V1ActionSpecActionOutput[];
};

export function normalizeV1ActionResult(wire: ActionLaunchWire): V1ActionResult {
  if (wire.schema_version !== 'frisket.action_result.v1') {
    throw new ApiError(500, 'Unexpected action result schema');
  }
  const { errors, outputs, schema_version: schemaVersion, ...generatedFields } = wire;
  const normalizedOutputs = outputs?.map((output) => ({
    ...output,
    name: output.name ?? null,
  }));
  return {
    ...generatedFields,
    schema_version: schemaVersion,
    errors: errors ?? [],
    ...(normalizedOutputs === undefined ? {} : { outputs: normalizedOutputs }),
  };
}

const V1_ACTION_SCHEMA_VERSION = 'frisket.action.v2';

export interface ResolvedRunActionInvocation {
  readonly projectId: string;
  readonly signal?: AbortSignal;
}

export function resolveRunActionInvocation(
  options?: RunActionInvocationOptions,
  defaultProjectId?: string,
): ResolvedRunActionInvocation {
  const projectId = options?.projectId ?? defaultProjectId;
  if (!projectId) throw new ApiError(400, 'Project id is required');
  return {
    projectId,
    ...(options?.signal ? { signal: options.signal } : {}),
  };
}

export function actionAbortReason(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException('The operation was aborted', 'AbortError');
}

export function throwIfActionAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw actionAbortReason(signal);
}

/** Observe a shared producer through one caller's cancellation without ever
 * binding that producer (or its cache entry) to the caller's signal. */
export function observeWithActionSignal<T>(
  promise: Promise<T>,
  signal?: AbortSignal,
): Promise<T> {
  if (!signal) return promise;
  throwIfActionAborted(signal);
  return new Promise<T>((resolve, reject) => {
    const onAbort = (): void => {
      cleanup();
      reject(actionAbortReason(signal));
    };
    const cleanup = (): void => signal.removeEventListener('abort', onAbort);
    signal.addEventListener('abort', onAbort, { once: true });
    promise.then(
      (value) => {
        cleanup();
        resolve(value);
      },
      (error: unknown) => {
        cleanup();
        reject(error);
      },
    );
  });
}

function canonicalSheetRowScope(rowScope: SheetRowScope): SheetRowScope {
  if (rowScope.selector.kind === 'all_rows') return rowScope;
  return {
    sheet_id: rowScope.sheet_id,
    selector: {
      kind: 'exact_membership',
      membership: {
        row_ids: [...new Set(rowScope.selector.membership.row_ids)]
          .sort((left, right) => left - right),
      },
    },
  };
}

export function v1ActionSpecEnvelope({
  kind,
  capabilities,
  params,
  idempotencyKey,
  rowScope,
}: {
  kind: string;
  capabilities: string[];
  params: Record<string, unknown>;
  idempotencyKey: string;
  rowScope?: SheetRowScope;
}): Record<string, unknown> {
  const canonicalRowScope = rowScope && canonicalSheetRowScope(rowScope);
  return {
    schema_version: V1_ACTION_SCHEMA_VERSION,
    kind,
    capabilities,
    params,
    ...(canonicalRowScope ? { row_scope: canonicalRowScope } : {}),
    idempotency_key: idempotencyKey,
  };
}

export interface V1ActionResultPostOptions {
  clearIdempotency?: 'afterPost' | 'finally' | 'never' | 'onSuccess';
  postMode?: 'default' | 'operation';
  operationInvocation?: ResolvedRunActionInvocation;
}

const newV1IdempotencyKey = (actionKind: string): string => {
  const random =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `web-${actionKind}:${random}`;
};

// Ruling 4 (a retry is a resume; everything else is a deliberate NEW
// purchase): after a request SUCCEEDS its idempotency key lingers for this
// window before a fresh key is minted for the same semantic request.
const V1_ACTION_KEY_REPLAY_WINDOW_MS = 5000;

function specKindAndParams(
  spec: Record<string, unknown>,
): {
  kind: string;
  params: Record<string, unknown>;
  rowScope?: SheetRowScope;
} | null {
  const kind = typeof spec.action_id === 'string' ? spec.action_id : spec.kind;
  const params = spec.params;
  if (
    typeof kind !== 'string' ||
    params === null ||
    typeof params !== 'object' ||
    Array.isArray(params)
  ) {
    return null;
  }
  const rowScope = spec.row_scope;
  if (
    rowScope !== undefined
    && (rowScope === null || typeof rowScope !== 'object' || Array.isArray(rowScope))
  ) return null;
  return {
    kind,
    params: typeof spec.action_id === 'string'
      ? {
          params,
          scope: spec.scope,
          output_names: spec.output_names ?? {},
          ...(spec.sheet_name == null ? {} : { sheet_name: spec.sheet_name }),
        }
      : params as Record<string, unknown>,
    ...(rowScope === undefined ? {} : { rowScope: rowScope as SheetRowScope }),
  };
}

/** Per-API pending-idempotency-key policy for the double-buy guard. */
export class V1ActionIdempotencyKeys {
  private pending = new Map<string, string>();

  private signature(
    actionKind: string,
    params: Record<string, unknown>,
    projectId?: string,
    rowScope?: SheetRowScope,
  ): string {
    const semantic = v1ActionIdempotencySignature(actionKind, params, rowScope);
    return projectId === undefined
      ? semantic
      : JSON.stringify({ projectId, semantic });
  }

  keyFor(
    actionKind: string,
    params: Record<string, unknown>,
    projectId?: string,
    rowScope?: SheetRowScope,
  ): string {
    const signature = this.signature(actionKind, params, projectId, rowScope);
    const existing = this.pending.get(signature);
    if (existing) return existing;
    const key = newV1IdempotencyKey(actionKind);
    this.pending.set(signature, key);
    return key;
  }

  clear(
    actionKind: string,
    params: Record<string, unknown>,
    projectId?: string,
    rowScope?: SheetRowScope,
  ): void {
    this.pending.delete(this.signature(actionKind, params, projectId, rowScope));
  }

  release(
    actionKind: string,
    params: Record<string, unknown>,
    windowMs?: number,
  ): void;
  release(
    actionKind: string,
    params: Record<string, unknown>,
    projectId: string,
    windowMs?: number,
    rowScope?: SheetRowScope,
  ): void;
  release(
    actionKind: string,
    params: Record<string, unknown>,
    projectIdOrWindowMs?: string | number,
    windowMs = V1_ACTION_KEY_REPLAY_WINDOW_MS,
    rowScope?: SheetRowScope,
  ): void {
    const projectId = typeof projectIdOrWindowMs === 'string'
      ? projectIdOrWindowMs
      : undefined;
    const replayWindowMs = typeof projectIdOrWindowMs === 'number'
      ? projectIdOrWindowMs
      : windowMs;
    const signature = this.signature(actionKind, params, projectId, rowScope);
    const key = this.pending.get(signature);
    if (!key) return;
    setTimeout(() => {
      if (this.pending.get(signature) === key) this.pending.delete(signature);
    }, replayWindowMs);
  }
}

export const v1ActionIdempotencySignature = (
  actionKind: string,
  params: Record<string, unknown>,
  rowScope?: SheetRowScope,
): string => {
  const identityParams = { ...params };
  delete identityParams.confirmed;
  delete identityParams.consented_promise_set_hash;
  return canonicalJson({
    actionKind,
    params: identityParams,
    ...(rowScope ? { rowScope: canonicalSheetRowScope(rowScope) } : {}),
  });
};

export interface V1ActionSession {
  v1ActionSpec(
    kind: string,
    capabilities: string[],
    params: Record<string, unknown>,
    projectId?: string,
    signal?: AbortSignal,
    rowScope?: SheetRowScope,
  ): Record<string, unknown>;
  registeredProjectActionSpec(
    actionId: string,
    params: Record<string, JsonValue>,
    projectId?: string,
    signal?: AbortSignal,
    names?: Partial<Pick<RegisteredActionRequest, 'sheet_name' | 'output_names'>>,
  ): RegisteredActionRequest;
  registeredActionSpec(
    draft: RegisteredActionDraft,
    projectId?: string,
    signal?: AbortSignal,
  ): RegisteredActionRequest;
  clearV1ActionIdempotencyKey(
    spec: Record<string, unknown>,
    explicitProjectId?: string,
  ): void;
  releaseV1ActionIdempotencyKey(
    spec: Record<string, unknown>,
    explicitProjectId?: string,
  ): void;
  postV1ActionSpec(
    spec: Record<string, unknown>,
    options?: RunActionInvocationOptions,
  ): Promise<V1ActionResult>;
  withV1ActionResult<T>(
    spec: Record<string, unknown>,
    handler: (result: V1ActionResult) => T | Promise<T>,
    options?: V1ActionResultPostOptions,
  ): Promise<T>;
  assertCompletedV1ActionResult(
    result: V1ActionResult,
    fallbackMessage: string,
    status?: number,
  ): void;
}

class V1ActionSessionImpl implements V1ActionSession {
  private readonly pendingV1ActionKeys = new V1ActionIdempotencyKeys();
  private readonly v1ActionSpecProjects = new WeakMap<Record<string, unknown>, string>();
  private readonly projectId: string;
  private readonly actionLaunchApi: ReturnType<typeof createActionLaunchApi>;

  constructor(projectId: string) {
    this.projectId = projectId;
    this.actionLaunchApi = createActionLaunchApi(actionLaunchErrorFromContract, projectId);
  }

  v1ActionSpec(
    kind: string,
    capabilities: string[],
    params: Record<string, unknown>,
    projectId = this.projectId,
    signal?: AbortSignal,
    rowScope?: SheetRowScope,
  ): Record<string, unknown> {
    throwIfActionAborted(signal);
    const spec = v1ActionSpecEnvelope({
      kind,
      capabilities,
      params,
      idempotencyKey: this.pendingV1ActionKeys.keyFor(
        kind,
        params,
        projectId,
        rowScope,
      ),
      rowScope,
    });
    throwIfActionAborted(signal);
    this.v1ActionSpecProjects.set(spec, projectId);
    return spec;
  }

  registeredProjectActionSpec(
    actionId: string,
    params: Record<string, JsonValue>,
    projectId = this.projectId,
    signal?: AbortSignal,
    names: Partial<Pick<RegisteredActionRequest, 'sheet_name' | 'output_names'>> = {},
  ): RegisteredActionRequest {
    const draft: RegisteredActionDraft = {
      action_id: actionId,
      scope: { kind: 'project' },
      params,
      output_names: names.output_names ?? {},
      ...(names.sheet_name == null ? {} : { sheet_name: names.sheet_name }),
    };
    return this.registeredActionSpec(draft, projectId, signal);
  }

  registeredActionSpec(
    draft: RegisteredActionDraft,
    projectId = this.projectId,
    signal?: AbortSignal,
  ): RegisteredActionRequest {
    throwIfActionAborted(signal);
    const semantic = specKindAndParams(draft)!;
    const spec: RegisteredActionRequest = {
      ...draft,
      idempotency_key: this.pendingV1ActionKeys.keyFor(draft.action_id, semantic.params, projectId),
    };
    throwIfActionAborted(signal);
    this.v1ActionSpecProjects.set(spec, projectId);
    return spec;
  }

  clearV1ActionIdempotencyKey(
    spec: Record<string, unknown>,
    explicitProjectId?: string,
  ): void {
    const parsed = specKindAndParams(spec);
    const projectId = explicitProjectId ?? this.v1ActionSpecProjects.get(spec);
    if (parsed && projectId !== undefined) {
      this.pendingV1ActionKeys.clear(
        parsed.kind,
        parsed.params,
        projectId,
        parsed.rowScope,
      );
    }
  }

  releaseV1ActionIdempotencyKey(
    spec: Record<string, unknown>,
    explicitProjectId?: string,
  ): void {
    const parsed = specKindAndParams(spec);
    const projectId = explicitProjectId ?? this.v1ActionSpecProjects.get(spec);
    if (parsed && projectId !== undefined) {
      this.pendingV1ActionKeys.release(
        parsed.kind,
        parsed.params,
        projectId,
        undefined,
        parsed.rowScope,
      );
    }
  }

  async postV1ActionSpec(
    spec: Record<string, unknown>,
    options?: RunActionInvocationOptions,
  ): Promise<V1ActionResult> {
    const invocation = resolveRunActionInvocation(options, this.projectId);
    throwIfActionAborted(invocation.signal);
    const response = await this.actionLaunchApi.launch(spec as ActionLaunchRequest, {
      projectId: invocation.projectId,
      signal: invocation.signal,
    });
    if (response.status !== 200) {
      throw actionLaunchErrorFromContract(response.status, response.result);
    }
    throwIfActionAborted(invocation.signal);
    return normalizeV1ActionResult(response.result);
  }

  async withV1ActionResult<T>(
    spec: Record<string, unknown>,
    handler: (result: V1ActionResult) => T | Promise<T>,
    options: V1ActionResultPostOptions = {},
  ): Promise<T> {
    const clearIdempotency = options.clearIdempotency ?? 'onSuccess';
    const projectId = this.v1ActionSpecProjects.get(spec);
    const invocation = projectId === undefined ? undefined : { projectId };
    let succeeded = false;
    try {
      const out = options.postMode === 'operation'
        ? await this.postV1OperationActionSpec(spec, options.operationInvocation)
        : await this.postV1ActionSpec(spec, invocation);
      if (clearIdempotency === 'afterPost') {
        this.releaseV1ActionIdempotencyKey(spec);
      }
      try {
        const result = await handler(out);
        succeeded = true;
        if (clearIdempotency === 'onSuccess') {
          this.releaseV1ActionIdempotencyKey(spec);
        }
        return result;
      } catch (err) {
        if (clearIdempotency === 'afterPost') {
          this.clearV1ActionIdempotencyKey(spec);
        }
        throw err;
      }
    } finally {
      if (clearIdempotency === 'finally') {
        if (succeeded) this.releaseV1ActionIdempotencyKey(spec);
        else this.clearV1ActionIdempotencyKey(spec);
      }
    }
  }

  assertCompletedV1ActionResult(
    result: V1ActionResult,
    fallbackMessage: string,
    status = 400,
  ): void {
    if (result.status !== 'completed') {
      throw new ApiError(status, v1ErrorMessage(result, fallbackMessage));
    }
  }

  private async postV1OperationActionSpec(
    spec: Record<string, unknown>,
    invocation?: ResolvedRunActionInvocation,
  ): Promise<V1ActionResult> {
    const projectId = invocation?.projectId ?? this.v1ActionSpecProjects.get(spec);
    const signal = invocation?.signal;
    throwIfActionAborted(signal);
    const response = await this.actionLaunchApi.launch(spec as ActionLaunchRequest, {
      ...(projectId === undefined ? {} : { projectId }),
      ...(signal === undefined ? {} : { signal }),
    });
    throwIfActionAborted(signal);
    return normalizeV1ActionResult(response.result);
  }

}

export function createV1ActionSession(projectId: string): V1ActionSession {
  return new V1ActionSessionImpl(projectId);
}
