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

/** Evidence reads consumed by the workbench's keyed evidence resource. */
export type ColumnCitationsApiPort = Pick<ProjectApi, 'getColumnEvidence'>;

/** Evidence viewer reads consumed by the workbench's keyed viewer resource. */
export type CitationViewerApiPort = Pick<ProjectApi, 'getEvidenceViewer'>;

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

export type SourceApiPort = Pick<
  ProjectApi,
  | 'listSources'
  | 'getSource'
  | 'getSourceHealth'
  | 'createSource'
  | 'updateSource'
  | 'deleteSource'
  | 'fetchSource'
>;
