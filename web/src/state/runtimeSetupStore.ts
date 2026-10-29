import { createStore } from '../core/store/createStore';
import type { ModelPullDto } from '../api/types';
import type { ModelSetupScope } from '../api/engineSetup';

/** Workspace-owned operation: closing an action must not stop its observer. */
export function createRuntimeSetupStore() {
  const store = createStore<{
    pull: ModelPullDto | null;
    scope: ModelSetupScope;
    completionVersion: number;
    canMutate: boolean;
  }>({ pull: null, scope: 'workspace', completionVersion: 0, canMutate: false });
  return {
    store,
    observe(pull: ModelPullDto, scope: ModelSetupScope, canMutate: boolean) {
      store.set((state) => ({ ...state, pull, scope, canMutate,
        completionVersion: state.completionVersion + (
          pull.status === 'done' && (state.pull?.id !== pull.id || state.pull.status !== 'done') ? 1 : 0
        ),
      }));
    },
    dismiss() {
      store.set((state) => ({ ...state, pull: null }));
    },
    update(pull: ModelPullDto) {
      store.set((state) => state.pull?.id !== pull.id ? state : ({
        ...state, pull,
        completionVersion: state.completionVersion
          + (!['pending', 'running'].includes(pull.status) && ['pending', 'running'].includes(state.pull.status) ? 1 : 0),
      }));
    },
  };
}
export type RuntimeSetupStoreHandle = ReturnType<typeof createRuntimeSetupStore>;
