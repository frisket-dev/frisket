import type { ComponentType } from 'react';
import type { PluginPanelContext } from './pluginPanelContext';
import { SELECTION_SUMMARY_PANEL_COMPONENT_KEY, SelectionSummaryPanel } from './SelectionSummaryPanel';

// The binding seam between checked-in descriptor DATA
// (src/frisket/data/first_party_workbench_descriptors.json, which
// deliberately excludes componentKey/handlerKey — RUNTIME_ONLY_FIELDS in
// workbench/contracts.py) and the natively-bundled components/handlers that
// render them. Every id in the artifact has an entry here
// (tests/server/test_first_party_descriptor_package.py source-scans this file).
// This is the "component binding resolves against the in-bundle registry
// instead of requiring a moduleUrl" trust difference from
// pluginRuntimeDescriptors.ts.
export interface FirstPartyComponentBinding {
  componentKey: string;
  handlerKey?: string;
}

const FIRST_PARTY_COMPONENT_BINDINGS: Readonly<Record<string, FirstPartyComponentBinding>> = {
  'frisket.investigative.panel.friendly_filters': { componentKey: 'core.panels.FriendlyFiltersPanel' },
  'frisket.investigative.panel.mentions': { componentKey: 'core.panels.MentionsPanel' },
  'frisket.core.panel.sources': { componentKey: 'core.panels.SourcesPanel' },
  'frisket.core.panel.search': { componentKey: 'core.panels.SearchPanel' },
  'frisket.core.panel.copilot': { componentKey: 'core.panels.CopilotPanel' },
  'frisket.core.panel.notifications': { componentKey: 'core.panels.NotificationsPanel' },
  'frisket.core.panel.saved_views': { componentKey: 'core.panels.SavedViewsPanel' },
  'frisket.core.panel.watches': { componentKey: 'core.panels.WatchesPanel' },
  'frisket.embeddings.panel.indexes': { componentKey: 'embeddings.panels.EmbeddingsPanel' },
  // No 'frisket.core.panel.actions' binding: actions are expressed by the
  // shell's overlay ActionDrawer (which mounts ActionPanel directly via
  // LazyActionPanel in App.tsx), not placed through the descriptor pipeline.
  'frisket.core.panel.history': { componentKey: 'core.panels.HistoryPanel' },
  'frisket.core.panel.lineage': { componentKey: 'core.bottomDock.LineagePanel' },
  // No 'frisket.core.panel.projection_status' binding: the projection-status
  // panel retired to a status-bar chip and its bound component never
  // existed — the descriptor trio was removed.
  // No 'frisket.core.panel.plugins' binding: the dock's plugin-manager tab
  // retired — it was a read-only duplicate of the full manager Settings
  // already hosts. The descriptor entry was removed from the checked-in
  // artifact too (src/frisket/data/first_party_workbench_descriptors.json),
  // same disposal as the projection_status panel above.
  'frisket.core.panel.jobs': { componentKey: 'core.bottomDock.JobsPanel' },
  'frisket.core.panel.errors': { componentKey: 'core.bottomDock.ErrorsPanel' },
  'frisket.core.command.open_sources': {
    componentKey: 'core.commands.OpenSources',
    handlerKey: 'core.commands.openContribution',
  },
  'frisket.core.command.open_settings': {
    componentKey: 'core.commands.OpenSettings',
    handlerKey: 'core.commands.navigate',
  },
  'frisket.media.command.ocr_compare': {
    componentKey: 'media.commands.OcrCompare',
    handlerKey: 'media.commands.openOcrCompare',
  },
  'frisket.media.command.transcribe_compare': {
    componentKey: 'media.commands.TranscribeCompare',
    handlerKey: 'media.commands.openTranscribeCompare',
  },
  'frisket.media.command.translate_compare': {
    componentKey: 'media.commands.TranslateCompare',
    handlerKey: 'media.commands.openTranslateCompare',
  },
  'frisket.media.command.topic_segmentation_compare': {
    componentKey: 'media.commands.TopicSegmentationCompare',
    handlerKey: 'media.commands.openTopicSegmentationCompare',
  },
  'frisket.core.view.grid': { componentKey: 'core.views.SheetGrid' },
  'frisket.media.view.image_gallery': { componentKey: 'media.views.ImageGallery' },
  'frisket.core.view.evidence': { componentKey: 'core.views.EvidenceViewer' },
  'frisket.core.view.review_queue': { componentKey: 'core.views.ReviewQueue' },
  'frisket.core.panel.provenance': { componentKey: 'core.panels.ProvenanceManifest' },
  'frisket.core.view.row_delete_confirm': { componentKey: 'core.views.ConfirmDeleteRowsModal' },
  'frisket.core.view.cost_gate': { componentKey: 'core.views.CostGateModal' },
  'frisket.core.view.output_column_collision': {
    componentKey: 'core.views.OutputColumnCollisionModal',
  },
  'frisket.investigative.view.graph_neighborhood': {
    componentKey: 'investigative.views.GraphNeighborhoodView',
  },
  'frisket.core.row_inspector.section.evidence': { componentKey: 'core.rowInspector.CellEvidenceBlock' },
  'frisket.core.column_inspector.section.settings': { componentKey: 'core.columnInspector.ColumnSettings' },
  'frisket.core.column_inspector.section.runs': { componentKey: 'core.columnInspector.ColumnRuns' },
  'frisket.investigative.view.entity_summary': { componentKey: 'investigative.entityDetail.Summary' },
  'frisket.investigative.view.entity_evidence': { componentKey: 'investigative.entityDetail.Evidence' },
  'frisket.investigative.view.entity_connections': {
    componentKey: 'investigative.entityDetail.Connections',
  },
  'frisket.core.view.source_summary': { componentKey: 'core.sourceDetail.Summary' },
  'frisket.core.view.source_health': { componentKey: 'core.sourceDetail.Health' },
  'frisket.core.view.source_runs': { componentKey: 'core.sourceDetail.Runs' },
  'frisket.core.source_kind_form.rss': { componentKey: 'core.sources.RssSourceForm' },
  'frisket.core.source_kind_form.api_list_dicts': { componentKey: 'core.sources.ApiListDictsSourceForm' },
};

export function firstPartyComponentBinding(contributionId: string): FirstPartyComponentBinding | null {
  return FIRST_PARTY_COMPONENT_BINDINGS[contributionId] ?? null;
}

// componentKeys that render through a natively-bundled React component when
// a descriptor carries no runtimeComponent (no moduleUrl). This absorbs
// PluginPanelHost's former SELECTION_SUMMARY_PANEL_COMPONENT_KEY special
// case (a trusted-local demo panel that never gained a served module) into
// the same lookup mechanism instead of a hardcoded componentKey comparison.
const NATIVE_PANEL_COMPONENTS: Readonly<Record<string, ComponentType<{ ctx: PluginPanelContext }>>> = {
  [SELECTION_SUMMARY_PANEL_COMPONENT_KEY]: SelectionSummaryPanel,
};

export function nativePanelComponentForKey(
  componentKey: string,
): ComponentType<{ ctx: PluginPanelContext }> | null {
  return NATIVE_PANEL_COMPONENTS[componentKey] ?? null;
}
