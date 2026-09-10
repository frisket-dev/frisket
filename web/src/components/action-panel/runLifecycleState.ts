import type { ParamValidationResult, RunEstimate } from '../../api/types';

/** Display state keyed to the controller's request identity. This module is
 * presentation state only, never readiness or request authority — and no longer
 * holds a pending confirmation: the cost gate is the run controller's, opened on
 * the server's 402 (state/jobStore.ts), so the panel neither decides that a run
 * needs confirming nor owns the dialog that asks. */
export interface RunLifecycleState {
  v1Estimate: { requestKey: string; estimate: RunEstimate } | null;
  paramValidation: { requestKey: string; diagnostics: ParamValidationResult } | null;
  overwriteColumnName: string | null;
}

export const INITIAL_RUN_LIFECYCLE_STATE: RunLifecycleState = {
  v1Estimate: null,
  paramValidation: null,
  overwriteColumnName: null,
};

export type RunLifecycleAction =
  | { type: 'estimateResolved'; requestKey: string; estimate: RunEstimate }
  | { type: 'estimateFetchFailed'; requestKey: string }
  | { type: 'paramValidationResolved'; requestKey: string; diagnostics: ParamValidationResult }
  | { type: 'paramValidationFetchFailed'; requestKey: string }
  | { type: 'overwriteArmed'; columnName: string };

export function runLifecycleReducer(
  state: RunLifecycleState,
  action: RunLifecycleAction,
): RunLifecycleState {
  switch (action.type) {
    case 'estimateResolved':
      return {
        ...state,
        v1Estimate: { requestKey: action.requestKey, estimate: action.estimate },
      };
    case 'estimateFetchFailed':
      return state.v1Estimate?.requestKey === action.requestKey
        ? { ...state, v1Estimate: null }
        : state;
    case 'paramValidationResolved':
      return {
        ...state,
        paramValidation: { requestKey: action.requestKey, diagnostics: action.diagnostics },
      };
    case 'paramValidationFetchFailed':
      return state.paramValidation?.requestKey === action.requestKey
        ? { ...state, paramValidation: null }
        : state;
    case 'overwriteArmed':
      return { ...state, overwriteColumnName: action.columnName };
    default:
      return state;
  }
}
