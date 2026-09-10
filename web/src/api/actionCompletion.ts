import {
  actionAbortReason,
  throwIfActionAborted,
  type ResolvedRunActionInvocation,
  type V1ActionSpecActionOutput,
  type V1ActionResult,
} from './v1ActionSession';
import { ApiError, v1ErrorMessage } from './contractErrors';
import {
  getReceiptContract,
  getRunProgressContract,
} from './httpContractRoutes';
import type { V1Receipt } from './types';

const ACTION_COMPLETION_POLL_INTERVAL_MS = 1000;

type ContractErrorFactory = (status: number, payload: unknown) => Error;

export interface ActionCompletionApi {
  waitForRunCompletion(
    result: V1ActionResult,
    fallbackMessage: string,
    invocation: ResolvedRunActionInvocation,
  ): Promise<V1ActionResult>;
}

/** Receipt refs are the durable output truth once a queued run completes. */
export function receiptOutputsAsActionOutputs(
  receipt: V1Receipt,
): V1ActionSpecActionOutput[] {
  return receipt.outputs.map((output) => {
    const ref = output.ref ?? {};
    const kind = typeof ref.kind === 'string' ? ref.kind : 'output';
    const rowIds = Array.isArray(ref.row_ids)
      ? ref.row_ids.filter((rowId): rowId is number => typeof rowId === 'number')
      : undefined;
    return {
      kind,
      name: output.name,
      sheet_id: typeof ref.sheet_id === 'number' ? ref.sheet_id : null,
      column_id: typeof ref.column_id === 'number' ? ref.column_id : null,
      ...(rowIds ? { row_ids: rowIds } : {}),
      ref,
    };
  });
}

function waitForPollInterval(signal?: AbortSignal): Promise<void> {
  if (!signal) {
    return new Promise((resolve) => {
      window.setTimeout(resolve, ACTION_COMPLETION_POLL_INTERVAL_MS);
    });
  }
  throwIfActionAborted(signal);
  return new Promise((resolve, reject) => {
    let timer: number | null = window.setTimeout(() => {
      timer = null;
      cleanup();
      resolve();
    }, ACTION_COMPLETION_POLL_INTERVAL_MS);
    const onAbort = (): void => {
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
      cleanup();
      reject(actionAbortReason(signal));
    };
    const cleanup = (): void => signal.removeEventListener('abort', onAbort);
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

export function createActionCompletionApi(
  errorFactory: ContractErrorFactory,
): ActionCompletionApi {
  async function pollRunCompletion(
    result: V1ActionResult,
    runId: string,
    receiptId: string,
    fallbackMessage: string,
    invocation: ResolvedRunActionInvocation,
  ): Promise<V1ActionResult> {
    throwIfActionAborted(invocation.signal);
    const progress = await getRunProgressContract(
      invocation.projectId,
      Number(runId),
      errorFactory,
      { signal: invocation.signal },
    );
    throwIfActionAborted(invocation.signal);
    if (progress.status === 'failed' || progress.status === 'cancelled') {
      throw new ApiError(500, `${fallbackMessage}: ${progress.status}`);
    }
    if (progress.status === 'stalled' || progress.status === 'orphaned') {
      throw new ApiError(500, `${fallbackMessage}: ${progress.status}`);
    }
    if (progress.status === 'complete') {
      throwIfActionAborted(invocation.signal);
      const receipt = await getReceiptContract(
        invocation.projectId,
        receiptId,
        errorFactory,
        { signal: invocation.signal },
      );
      throwIfActionAborted(invocation.signal);
      if (receipt.status !== 'completed') {
        const message = v1ErrorMessage(
          receipt,
          `${fallbackMessage}: receipt ${receipt.status}`,
        );
        throw new ApiError(500, message);
      }
      return {
        ...result,
        status: 'completed',
        outputs: receiptOutputsAsActionOutputs(receipt),
        errors: [],
      };
    }
    await waitForPollInterval(invocation.signal);
    throwIfActionAborted(invocation.signal);
    return pollRunCompletion(
      result,
      runId,
      receiptId,
      fallbackMessage,
      invocation,
    );
  }

  return {
    async waitForRunCompletion(result, fallbackMessage, invocation) {
      throwIfActionAborted(invocation.signal);
      if (result.status === 'completed') return result;
      if (result.run_id == null) {
        throw new ApiError(500, v1ErrorMessage(result, fallbackMessage));
      }
      if (result.status !== 'queued' && result.status !== 'running') {
        throw new ApiError(500, v1ErrorMessage(result, fallbackMessage));
      }
      if (!result.receipt_id) {
        throw new ApiError(
          500,
          `${fallbackMessage}: queued action did not return a receipt`,
        );
      }
      return pollRunCompletion(
        result,
        String(result.run_id),
        result.receipt_id,
        fallbackMessage,
        invocation,
      );
    },
  };
}
