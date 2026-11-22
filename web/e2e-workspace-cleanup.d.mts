export declare const DEFAULT_PRUNE_HOURS: number;
export declare const DEFAULT_MAX_TOTAL_BYTES: number;
export declare const DEFAULT_BASE_KEEP_RUNS: number;

export declare function activeMarkerPath(workspace: string): string;

export declare function prepareE2EWorkspace(options: {
  workspace: string;
  runId: string;
  env?: NodeJS.ProcessEnv;
  tmpDir?: string;
}): void;

export declare function removeActiveMarker(workspace: string): void;

export declare function cleanupCurrentE2EWorkspace(options: {
  status?: string;
  env?: NodeJS.ProcessEnv;
  workspace?: string;
  log?: (...args: unknown[]) => void;
}): unknown;

export declare function enforceE2EWorkspaceSizeCap(options?: {
  env?: NodeJS.ProcessEnv;
  tmpDir?: string;
  currentWorkspace?: string;
  now?: number;
  log?: (...args: unknown[]) => void;
}): unknown;

export declare function pruneStaleE2EWorkspaces(options?: {
  env?: NodeJS.ProcessEnv;
  tmpDir?: string;
  currentWorkspace?: string;
  now?: number;
  log?: (...args: unknown[]) => void;
}): unknown;

export declare function pruneStaleBaseWorkspaceProjects(options: {
  workspace: string;
  env?: NodeJS.ProcessEnv;
  runId?: string;
  now?: number;
  log?: (...args: unknown[]) => void;
}): unknown;
