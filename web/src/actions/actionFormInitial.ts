import type { GeneratedActionDraft } from '../api/types';
import type { ActionComparisonContext } from '../api/actionComparison';

export interface ActionCompareCapturedSelection {
  rowIds: number[];
  totalCount: number;
}

interface ActionCompareLaunchBase {
  /** The source sheet remains explicit so a later live-grid navigation cannot
   * retarget final execution. `context.sheet` is the canonical identity. */
  context: ActionComparisonContext;
  capturedSelection: ActionCompareCapturedSelection | null;
  sampleRowIds: number[];
  /** Presentation intent only. The callback itself stays on ActionPanel. */
  returnToCompare: boolean;
}

/** Browser-only comparison/form handoff. It is not part of a saved action
 * draft and carries no confirmation or reusable execution authority. */
export type ActionCompareLaunchContext =
  | ActionCompareLaunchBase & {
      kind: 'edit-existing';
    }
  | ActionCompareLaunchBase & {
      kind: 'use-comparison-variant';
      variantId: string;
      variantLabel: string;
    };

/** Browser-only launch hints. They initialize a form and never enter an API request. */
export interface ActionLaunchPrefill {
  sourceColumn?: string;
  prompt?: string;
  /** Reusable typed intent from a completed result; never invocation consent. */
  actionDraft?: GeneratedActionDraft;
  /** Transient comparison/Replace presentation and target fences. */
  actionCompare?: ActionCompareLaunchContext;
}
