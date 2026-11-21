import type { createProjectApi } from './real';

/** Everything the workspace does against one project, as composed by
 * createProjectApi. */
export type ProjectApi = ReturnType<typeof createProjectApi>;

export type GridApiPort = Pick<
  ProjectApi,
  | 'getSheetData'
  | 'getSheetRows'
  | 'getColumnStats'
  | 'locateSheetRow'
  | 'addRow'
  | 'addColumn'
  | 'deleteRows'
  | 'editCells'
  | 'acceptReplayValue'
  | 'acceptReplayValuesInColumn'
  | 'dismissReplayPending'
  | 'updateColumn'
  | 'getColumnRuns'
  | 'getReviewedRunRevision'
>;

export type WorkbenchApiPort = Pick<
  ProjectApi,
  | 'getWorkbenchPluginRuntimeIndex'
  | 'getWorkbenchPluginSettings'
  | 'patchWorkbenchPluginSettings'
  | 'installLocalWorkbenchPlugin'
  | 'activateWorkbenchPlugin'
  | 'activateWorkbenchPluginBackend'
  | 'disableWorkbenchPlugin'
  | 'uninstallWorkbenchPlugin'
>;

/** Citation reads consumed by the workbench's keyed citation resource. */
export type ColumnCitationsApiPort = Pick<ProjectApi, 'getColumnCitations'>;

/** Citation viewer reads consumed by the workbench's keyed viewer resource. */
export type CitationViewerApiPort = Pick<ProjectApi, 'getCitationViewer'>;

/** Projection operations exposed to workbench plugin projection views. */
export type PluginProjectionViewApiPort = Pick<
  ProjectApi,
  | 'getRuntimeProjectionStatus'
  | 'buildRuntimeProjection'
  | 'readRuntimeProjectionArtifact'
  | 'getMapPointsArrowBuffer'
>;

export type EmbeddingApiPort = Pick<
  ProjectApi,
  | 'getSheetData'
  | 'embeddingIndexes'
  | 'embeddingProviderCatalog'
  | 'createEmbeddingIndex'
  | 'refreshEmbeddingIndex'
  | 'updateEmbeddingIndexPolicy'
  | 'exportEmbeddingIndex'
  | 'embeddingExportArtifactUrl'
  | 'runEmbeddingIndexAnalysis'
  | 'embeddingHybridPreview'
  | 'embeddingSimilarityPreview'
  | 'saveView'
  | 'listViews'
  | 'resolveView'
  | 'createAlert'
> & {
  /** Polled to completion after a queued create/refresh. */
  getActionJob?: ProjectApi['getActionJob'];
};

export type FeedApiPort = Pick<
  ProjectApi,
  | 'listFeeds'
  | 'getFeed'
  | 'getFeedHealth'
  | 'createFeed'
  | 'updateFeed'
  | 'deleteFeed'
  | 'fetchFeed'
>;
