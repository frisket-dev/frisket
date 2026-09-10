// Typed domain handle for the job store. Reads the job member off the current
// project's WorkspaceStores instance — the handle reference is stable for the
// lifetime of that project's stores, so it never churns a dep array within one
// project session.

import { useWorkspaceStores } from './useWorkspaceStores';
import type { JobStoreHandle } from '../state/jobStore';

export function useJobsHandle(): JobStoreHandle {
  return useWorkspaceStores().job;
}
