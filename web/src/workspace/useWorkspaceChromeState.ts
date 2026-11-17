// The real implementation lives at bind/useWorkspaceChromeState.ts (it needs
// bind/'s React-facing store access, which workspace/ files must not import
// directly per the substrate boundary rules). This file re-exports it — and
// every chrome type this module used to define — so callers
// (workspace/useWorkspaceModel.tsx, App.tsx, workbench/DocumentView.tsx,
// workbench/DocumentReader.tsx) see the same hook signature/return shape and
// exported types from this path.

export {
  useDocumentAlongsidePreference,
  useWorkspaceChromeState,
} from '../bind/useWorkspaceChromeState';

export {
  DISCOVER_TABS,
  type DiscoverTab,
  type DocumentViewFit,
  type DocumentViewLayout,
  type DocumentViewState,
  type DocumentViewVideoFit,
  type PromotedView,
  type WorkViewKind,
} from '../state/chromeStore';
