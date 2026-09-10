import { useCallback, useMemo, useReducer } from 'react';
import type { ProjectInfo } from '../api/open';
import { workbenchNonHideableContributionIds } from '../workbench/visibility';

// The ⌘K palette's visibility commands hide and reveal panes through this
// store, which is standalone and visibility-only rather than part of a
// workspace-preset snapshot.
const CONTRIBUTION_VISIBILITY_SCHEMA_VERSION = 'frisket.contribution_visibility.v1';

/** Retired Window-menu store (presets + layout profiles + move-between-hosts).
 *  We migrate its hidden-id list forward once, then sweep the key. */
const RETIRED_WORKSPACE_PRESETS_PREFIX = 'frisket:workspace-presets:';

function visibilityStorageKey(projectId: ProjectInfo['id']): string {
  return `frisket:contribution-visibility:${projectId}`;
}

function retiredWorkspacePresetsKey(projectId: ProjectInfo['id']): string {
  return `${RETIRED_WORKSPACE_PRESETS_PREFIX}${projectId}`;
}

function normalizeHiddenContributionIds(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  for (const entry of value) {
    if (typeof entry === 'string' && entry) seen.add(entry);
  }
  return [...seen];
}

/** Read a project's hidden-contribution ids, migrating once from the retired
 *  workspace-preset store and then removing that stale key (one-time cleanup
 *  sweep for the Window-menu retirement). */
function loadHiddenContributionIds(projectId: ProjectInfo['id']): string[] {
  let hidden: string[] | null = null;
  try {
    const raw = localStorage.getItem(visibilityStorageKey(projectId));
    if (raw !== null) {
      const parsed = JSON.parse(raw) as { hiddenContributionIds?: unknown };
      hidden = normalizeHiddenContributionIds(parsed.hiddenContributionIds);
    }
  } catch {
    hidden = null;
  }

  // One-time migration + cleanup of the retired workspace-preset key.
  try {
    const retiredRaw = localStorage.getItem(retiredWorkspacePresetsKey(projectId));
    if (retiredRaw !== null) {
      if (hidden === null) {
        const retired = JSON.parse(retiredRaw) as {
          currentSnapshot?: { hiddenContributionIds?: unknown };
        };
        hidden = normalizeHiddenContributionIds(retired.currentSnapshot?.hiddenContributionIds);
        persistHiddenContributionIds(projectId, hidden);
      }
      localStorage.removeItem(retiredWorkspacePresetsKey(projectId));
    }
  } catch {
    // ignore malformed retired state
  }

  return hidden ?? [];
}

function persistHiddenContributionIds(
  projectId: ProjectInfo['id'],
  hiddenContributionIds: string[],
): void {
  try {
    localStorage.setItem(
      visibilityStorageKey(projectId),
      JSON.stringify({
        schemaVersion: CONTRIBUTION_VISIBILITY_SCHEMA_VERSION,
        hiddenContributionIds,
      }),
    );
  } catch {
    // storage unavailable — hide/reveal still works for the session
  }
}

function hiddenIdsWith(hiddenContributionIds: string[], contributionId: string): string[] {
  if (!contributionId || workbenchNonHideableContributionIds().has(contributionId)) {
    return hiddenContributionIds;
  }
  return hiddenContributionIds.includes(contributionId)
    ? hiddenContributionIds
    : [...hiddenContributionIds, contributionId];
}

function hiddenIdsWithout(hiddenContributionIds: string[], contributionId: string): string[] {
  return hiddenContributionIds.filter((id) => id !== contributionId);
}

interface WorkbenchContributionVisibilityState {
  hiddenContributionIds: string[];
}

type WorkbenchContributionVisibilityAction = {
  hiddenContributionIds: string[];
  type: 'set';
};

function workbenchContributionVisibilityReducer(
  state: WorkbenchContributionVisibilityState,
  action: WorkbenchContributionVisibilityAction,
): WorkbenchContributionVisibilityState {
  switch (action.type) {
    case 'set':
      return { hiddenContributionIds: action.hiddenContributionIds };
    default:
      return state;
  }
}

export interface WorkbenchContributionVisibilityController {
  hiddenContributionIds: string[];
  /** Memoized Set view of `hiddenContributionIds` — pure selectors take DATA,
   *  not the `isContributionHidden` closure; this is the stable, comparable
   *  value they consume instead. Additive: `hiddenContributionIds` is
   *  unchanged for its other consumers. */
  hiddenContributionIdSet: ReadonlySet<string>;
  isContributionHidden(contributionId: string): boolean;
  hideContribution(contributionId: string): void;
  revealContribution(contributionId: string): void;
}

export function useWorkbenchContributionVisibility(
  projectId: ProjectInfo['id'],
): WorkbenchContributionVisibilityController {
  const [state, dispatch] = useReducer(
    workbenchContributionVisibilityReducer,
    projectId,
    (id) => ({ hiddenContributionIds: loadHiddenContributionIds(id) }),
  );
  const { hiddenContributionIds } = state;
  const hiddenContributionIdSet = useMemo(
    () => new Set(hiddenContributionIds),
    [hiddenContributionIds],
  );

  const applyHiddenIds = useCallback(
    (nextHiddenIds: string[]) => {
      dispatch({ type: 'set', hiddenContributionIds: nextHiddenIds });
      persistHiddenContributionIds(projectId, nextHiddenIds);
    },
    [projectId],
  );

  const hideContribution = useCallback(
    (contributionId: string) => {
      applyHiddenIds(hiddenIdsWith(hiddenContributionIds, contributionId));
    },
    [applyHiddenIds, hiddenContributionIds],
  );

  const revealContribution = useCallback(
    (contributionId: string) => {
      applyHiddenIds(hiddenIdsWithout(hiddenContributionIds, contributionId));
    },
    [applyHiddenIds, hiddenContributionIds],
  );

  return useMemo(
    () => ({
      hiddenContributionIds,
      hiddenContributionIdSet,
      hideContribution,
      isContributionHidden: (contributionId: string) =>
        hiddenContributionIds.includes(contributionId),
      revealContribution,
    }),
    [hiddenContributionIds, hiddenContributionIdSet, hideContribution, revealContribution],
  );
}
