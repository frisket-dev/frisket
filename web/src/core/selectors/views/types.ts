// The shape every core/selectors/views/<kind>.view.ts file default-exports.
// Framework-free (core/ boundary).

import type { WorkViewAvailabilityInput, WorkViewEntry, WorkViewKind } from '../workView';

export interface WorkViewDescriptor<K extends WorkViewKind = WorkViewKind> {
  /** Discriminant this descriptor handles; equals its key in the registry. */
  kind: K;
  /** Human title, used by promoteCurrentView (useWorkspaceModel.tsx). Every
   *  kind EXCEPT 'grid' must set this — the grid work view is never itself
   *  promoted/labelled (promoteCurrentView returns early for
   *  `activeWorkView === 'grid'`), mirroring the current
   *  `WORK_VIEW_TITLES: Record<Exclude<WorkViewKind, 'grid'>, string>` type
   *  this registry retires as a hand-maintained constant. */
  title?: string;
  /** Pure availability rule for this kind. Takes the SAME shared input every
   *  kind receives — self-sufficient, no cross-kind derived state threaded
   *  in, so each view file owns its own rule end to end. */
  computeEntry(input: WorkViewAvailabilityInput): WorkViewEntry;
}
