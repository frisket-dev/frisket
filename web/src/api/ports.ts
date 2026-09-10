import type { FrisketApi } from './types';

export interface GridApiPort {
  getSheetData: FrisketApi['getSheetData'];
  getColumnStats: FrisketApi['getColumnStats'];
  locateSheetRow: FrisketApi['locateSheetRow'];
  addRow: FrisketApi['addRow'];
  addColumn: FrisketApi['addColumn'];
  deleteRows: FrisketApi['deleteRows'];
  editCells: FrisketApi['editCells'];
  acceptReplayValue: FrisketApi['acceptReplayValue'];
  acceptReplayValuesInColumn: FrisketApi['acceptReplayValuesInColumn'];
  dismissReplayPending: FrisketApi['dismissReplayPending'];
  updateColumn: FrisketApi['updateColumn'];
  getColumnRuns: FrisketApi['getColumnRuns'];
}

export interface WorkbenchApiPort {
  getWorkbenchPluginRuntimeIndex: FrisketApi['getWorkbenchPluginRuntimeIndex'];
  getWorkbenchPluginSettings: FrisketApi['getWorkbenchPluginSettings'];
  patchWorkbenchPluginSettings: FrisketApi['patchWorkbenchPluginSettings'];
  installLocalWorkbenchPlugin: FrisketApi['installLocalWorkbenchPlugin'];
  activateWorkbenchPlugin: FrisketApi['activateWorkbenchPlugin'];
  activateWorkbenchPluginBackend: FrisketApi['activateWorkbenchPluginBackend'];
  disableWorkbenchPlugin: FrisketApi['disableWorkbenchPlugin'];
  uninstallWorkbenchPlugin: FrisketApi['uninstallWorkbenchPlugin'];
}

/** Evidence reads consumed by the workbench's keyed evidence resource. */
export interface ColumnEvidenceApiPort {
  getColumnEvidence: FrisketApi['getColumnEvidence'];
}

/** Evidence viewer reads consumed by the workbench's keyed viewer resource. */
export interface EvidenceLinkViewerApiPort {
  getEvidenceViewer: FrisketApi['getEvidenceViewer'];
}

/** Projection operations exposed to workbench plugin projection views. */
export interface PluginProjectionViewApiPort {
  getRuntimeProjectionStatus: FrisketApi['getRuntimeProjectionStatus'];
  buildRuntimeProjection: FrisketApi['buildRuntimeProjection'];
  readRuntimeProjectionArtifact: FrisketApi['readRuntimeProjectionArtifact'];
  getMapPointsArrowBuffer: FrisketApi['getMapPointsArrowBuffer'];
}

export interface EmbeddingApiPort {
  getSheetData: FrisketApi['getSheetData'];
  embeddingIndexes: FrisketApi['embeddingIndexes'];
  embeddingProviderCatalog: FrisketApi['embeddingProviderCatalog'];
  createEmbeddingIndex: FrisketApi['createEmbeddingIndex'];
  refreshEmbeddingIndex: FrisketApi['refreshEmbeddingIndex'];
  /** Polled to completion after a queued create/refresh. */
  getActionJob?: FrisketApi['getActionJob'];
  updateEmbeddingIndexPolicy: FrisketApi['updateEmbeddingIndexPolicy'];
  exportEmbeddingIndex: FrisketApi['exportEmbeddingIndex'];
  embeddingExportArtifactUrl: FrisketApi['embeddingExportArtifactUrl'];
  runEmbeddingIndexAnalysis: FrisketApi['runEmbeddingIndexAnalysis'];
  embeddingHybridPreview: FrisketApi['embeddingHybridPreview'];
  embeddingSimilarityPreview: FrisketApi['embeddingSimilarityPreview'];
  saveLens: FrisketApi['saveLens'];
  listLenses: FrisketApi['listLenses'];
  resolveLens: FrisketApi['resolveLens'];
  createWatch: FrisketApi['createWatch'];
}

export interface SourceApiPort {
  listSources: FrisketApi['listSources'];
  getSource: FrisketApi['getSource'];
  getSourceHealth: FrisketApi['getSourceHealth'];
  createSource: FrisketApi['createSource'];
  updateSource: FrisketApi['updateSource'];
  deleteSource: FrisketApi['deleteSource'];
  fetchSource: FrisketApi['fetchSource'];
}

export interface ProjectApiPort extends
  WorkbenchApiPort,
  GridApiPort,
  EmbeddingApiPort,
  SourceApiPort,
  PluginProjectionViewApiPort,
  ColumnEvidenceApiPort,
  EvidenceLinkViewerApiPort {
  getProject: FrisketApi['getProject'];
  updateCurrentProject: FrisketApi['updateCurrentProject'];
  getProjectRetention: FrisketApi['getProjectRetention'];
  updateProjectRetention: FrisketApi['updateProjectRetention'];
  getProjectNetworkPolicy: FrisketApi['getProjectNetworkPolicy'];
  updateProjectNetworkPolicy: FrisketApi['updateProjectNetworkPolicy'];
  getProjectSettings: FrisketApi['getProjectSettings'];
  updateProjectSettings: FrisketApi['updateProjectSettings'];
  compactProject: FrisketApi['compactProject'];
  getProjectProviderKeys: FrisketApi['getProjectProviderKeys'];
  setProjectProviderKey: FrisketApi['setProjectProviderKey'];
  validateProjectProviderKey: FrisketApi['validateProjectProviderKey'];
  deleteProjectProviderKey: FrisketApi['deleteProjectProviderKey'];
  getProjectSecrets: FrisketApi['getProjectSecrets'];
  setProjectSecret: FrisketApi['setProjectSecret'];
  deleteProjectSecret: FrisketApi['deleteProjectSecret'];
  listMcpServers: FrisketApi['listMcpServers'];
  createMcpServer: FrisketApi['createMcpServer'];
  updateMcpServer: FrisketApi['updateMcpServer'];
  deleteMcpServer: FrisketApi['deleteMcpServer'];
  testMcpServer: FrisketApi['testMcpServer'];
  listActionCatalog: FrisketApi['listActionCatalog'];
  runAction: FrisketApi['runAction'];
  runProposal: FrisketApi['runProposal'];
  listActionJobs: FrisketApi['listActionJobs'];
  getActionJob?: FrisketApi['getActionJob'];
  getRunProgress: FrisketApi['getRunProgress'];
  cancelRun: FrisketApi['cancelRun'];
  getReceipt: FrisketApi['getReceipt'];
  startPreview: FrisketApi['startPreview'];
  getPreview: FrisketApi['getPreview'];
  cancelPreview: FrisketApi['cancelPreview'];
  backfillColumn: FrisketApi['backfillColumn'];
  listSheets: FrisketApi['listSheets'];
  getSheetGraph: FrisketApi['getSheetGraph'];
  getLineage: FrisketApi['getLineage'];
  setSheetTitleColumn: FrisketApi['setSheetTitleColumn'];
  deleteSheet: FrisketApi['deleteSheet'];
  getHistory: FrisketApi['getHistory'];
  getReviewCount: FrisketApi['getReviewCount'];
  undo: FrisketApi['undo'];
  redo: FrisketApi['redo'];
  stepTo: FrisketApi['stepTo'];
  deleteProject: FrisketApi['deleteProject'];
  projectExportUrl: FrisketApi['projectExportUrl'];
  sheetDatasetExportUrl: FrisketApi['sheetDatasetExportUrl'];
  workLogExportUrl: FrisketApi['workLogExportUrl'];
  createWatch: FrisketApi['createWatch'];
  listWatches: FrisketApi['listWatches'];
  updateWatch: FrisketApi['updateWatch'];
  deleteWatch: FrisketApi['deleteWatch'];
  getWatchRuns: FrisketApi['getWatchRuns'];
  getWatchRunEvents: FrisketApi['getWatchRunEvents'];
  runWatch: FrisketApi['runWatch'];
  listColumnTypes: FrisketApi['listColumnTypes'];
  updateColumn: FrisketApi['updateColumn'];
  clusterPreview: FrisketApi['clusterPreview'];
  replaceRulesPreview: FrisketApi['replaceRulesPreview'];
  estimateAction: FrisketApi['estimateAction'];
  resolveActionParams: FrisketApi['resolveActionParams'];
  exportGoogleSheets: FrisketApi['exportGoogleSheets'];
  getReviewBundles: FrisketApi['getReviewBundles'];
  reviewItem: FrisketApi['reviewItem'];
  listNotifications: FrisketApi['listNotifications'];
  getNotificationsSummary: FrisketApi['getNotificationsSummary'];
  markNotificationsSeen: FrisketApi['markNotificationsSeen'];
  markNotificationRead: FrisketApi['markNotificationRead'];
  ackNotification: FrisketApi['ackNotification'];
  unackNotification: FrisketApi['unackNotification'];
  listNotificationChannels: FrisketApi['listNotificationChannels'];
  createNotificationChannel: FrisketApi['createNotificationChannel'];
  updateNotificationChannel: FrisketApi['updateNotificationChannel'];
  listNotificationRoutes: FrisketApi['listNotificationRoutes'];
  createNotificationRoute: FrisketApi['createNotificationRoute'];
  updateNotificationRoute: FrisketApi['updateNotificationRoute'];
  testNotificationRoute: FrisketApi['testNotificationRoute'];
  listNotificationDeliveryRequests: FrisketApi['listNotificationDeliveryRequests'];
  refreshSheet: FrisketApi['refreshSheet'];
  listAttemptReceipts: FrisketApi['listAttemptReceipts'];
  getProvenanceManifest: FrisketApi['getProvenanceManifest'];
  getCellEvidence: FrisketApi['getCellEvidence'];
  getRunTraceRow: FrisketApi['getRunTraceRow'];
  getRunRows: FrisketApi['getRunRows'];
  columnValuesPreview: FrisketApi['columnValuesPreview'];
  entityMentionsPreview: FrisketApi['entityMentionsPreview'];
  listViews: FrisketApi['listViews'];
  saveView: FrisketApi['saveView'];
  renameView: FrisketApi['renameView'];
  replaceViewDefinition: FrisketApi['replaceViewDefinition'];
  deleteView: FrisketApi['deleteView'];
  listOAuthConnections: FrisketApi['listOAuthConnections'];
  googleOAuthStartUrl: FrisketApi['googleOAuthStartUrl'];
  copilotChat: FrisketApi['copilotChat'];
  getTextAnnotations: FrisketApi['getTextAnnotations'];
  entityMentionDocuments: FrisketApi['entityMentionDocuments'];
  entityMentionOccurrences: FrisketApi['entityMentionOccurrences'];
  compareOcrScratch: FrisketApi['compareOcrScratch'];
  compareTranscribeScratch: FrisketApi['compareTranscribeScratch'];
  estimateOcrScratch: FrisketApi['estimateOcrScratch'];
  estimateTranscribeScratch: FrisketApi['estimateTranscribeScratch'];
  compareTopicSegmentationScratch: FrisketApi['compareTopicSegmentationScratch'];
  compareTranslateScratch: FrisketApi['compareTranslateScratch'];
}
