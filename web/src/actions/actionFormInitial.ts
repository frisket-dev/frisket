import type { GeneratedActionDraft } from '../api/types';

/** Browser-only launch hints. They initialize a form and never enter an API request. */
export interface ActionLaunchPrefill {
  sourceColumn?: string;
  prompt?: string;
  /** Reusable typed intent from a completed result; never invocation consent. */
  actionDraft?: GeneratedActionDraft;
}
