import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

// The product module is
// deliberately loaded through a guarded glob so the pre-extraction tree
// collects as an assertion-red rather than failing module resolution.
//
// What this file used to pin — the panel's unknown-cost preflight and its
// A/B settlement race — is gone with the confirmation itself. The panel no
// longer decides that a run needs confirming, so there is no pending confirm
// to keep request-keyed: the server's 402 opens the gate and the run
// controller owns it (state/jobStore.test.ts pins that wire). What is left
// here is display state, and the rule that still matters for it is the same
// one: a settlement is adopted only under its own request identity.
interface RunEstimate {
  cost: number | null;
  rows: number;
  llm: boolean;
}

interface RunLifecycleState {
  v1Estimate: { requestKey: string; estimate: RunEstimate } | null;
  paramValidation: { requestKey: string; diagnostics: Record<string, unknown> } | null;
  overwriteColumnName: string | null;
}

type RunLifecycleAction =
  | { type: 'estimateResolved'; requestKey: string; estimate: RunEstimate }
  | { type: 'estimateFetchFailed'; requestKey: string }
  | { type: 'paramValidationResolved'; requestKey: string; diagnostics: Record<string, unknown> }
  | { type: 'paramValidationFetchFailed'; requestKey: string }
  | { type: 'overwriteArmed'; columnName: string };

interface RunLifecycleModule {
  INITIAL_RUN_LIFECYCLE_STATE: RunLifecycleState;
  runLifecycleReducer(state: RunLifecycleState, action: RunLifecycleAction): RunLifecycleState;
}

const moduleUrl = new URL('../components/action-panel/runLifecycleState.ts', import.meta.url);
const loaders = import.meta.glob('../components/action-panel/runLifecycleState.ts');
const loader = loaders['../components/action-panel/runLifecycleState.ts'];
const moduleExists = existsSync(fileURLToPath(moduleUrl)) && Boolean(loader);
const loaded = loader ? ((await loader()) as RunLifecycleModule) : null;

function requireRunLifecycleModule(): RunLifecycleModule {
  expect(
    moduleExists && loaded !== null,
    'Implement web/src/components/action-panel/runLifecycleState.ts and export '
      + 'INITIAL_RUN_LIFECYCLE_STATE plus the pure runLifecycleReducer.',
  ).toBe(true);
  return loaded as RunLifecycleModule;
}

const estimate = (cost: number | null): RunEstimate => ({ cost, rows: 7, llm: true });

describe('pure request-keyed run lifecycle', () => {
  it('carries no pending confirmation at all — the server 402 owns the gate', () => {
    const { INITIAL_RUN_LIFECYCLE_STATE } = requireRunLifecycleModule();
    expect(INITIAL_RUN_LIFECYCLE_STATE).toEqual({
      v1Estimate: null,
      paramValidation: null,
      overwriteColumnName: null,
    });
  });

  it('adopts a resolved server estimate under its own request identity', () => {
    const { INITIAL_RUN_LIFECYCLE_STATE, runLifecycleReducer } = requireRunLifecycleModule();
    const next = runLifecycleReducer(INITIAL_RUN_LIFECYCLE_STATE, {
      type: 'estimateResolved', requestKey: 'A', estimate: estimate(1.25),
    });
    expect(next.v1Estimate).toEqual({ requestKey: 'A', estimate: estimate(1.25) });
  });

  it('clears failed display payloads only when their request keys match', () => {
    const { INITIAL_RUN_LIFECYCLE_STATE, runLifecycleReducer } = requireRunLifecycleModule();
    const withPayloads: RunLifecycleState = {
      ...INITIAL_RUN_LIFECYCLE_STATE,
      v1Estimate: { requestKey: 'new', estimate: estimate(3) },
      paramValidation: { requestKey: 'new', diagnostics: { valid: true } },
    };
    const staleEstimateFailure = runLifecycleReducer(withPayloads, {
      type: 'estimateFetchFailed', requestKey: 'old',
    });
    const staleValidationFailure = runLifecycleReducer(staleEstimateFailure, {
      type: 'paramValidationFetchFailed', requestKey: 'old',
    });
    expect(staleValidationFailure).toBe(withPayloads);

    const clearedEstimate = runLifecycleReducer(withPayloads, {
      type: 'estimateFetchFailed', requestKey: 'new',
    });
    expect(clearedEstimate.v1Estimate).toBeNull();
    const clearedBoth = runLifecycleReducer(clearedEstimate, {
      type: 'paramValidationFetchFailed', requestKey: 'new',
    });
    expect(clearedBoth.paramValidation).toBeNull();
  });

  it('is deterministic, does not mutate inputs, and accepts plain-data actions', () => {
    const { INITIAL_RUN_LIFECYCLE_STATE, runLifecycleReducer } = requireRunLifecycleModule();
    const state: RunLifecycleState = {
      ...INITIAL_RUN_LIFECYCLE_STATE,
      v1Estimate: { requestKey: 'A', estimate: estimate(1) },
    };
    const action: RunLifecycleAction = {
      type: 'estimateResolved', requestKey: 'A', estimate: estimate(4),
    };
    const stateSnapshot = structuredClone(state);
    const actionSnapshot = structuredClone(action);
    const first = runLifecycleReducer(state, action);
    const second = runLifecycleReducer(state, action);
    expect(second).toEqual(first);
    expect(state).toEqual(stateSnapshot);
    expect(action).toEqual(actionSnapshot);
    expect(JSON.parse(JSON.stringify(action))).toEqual(action);
    expect(Object.values(action).some((value) => typeof value === 'function')).toBe(false);
  });
});
