import type { SourceDetail, SourceInfo, SourceRun, SourceRunPage } from '../../api/open';
import { emptyForm, sourceToForm, type SourceFormState } from './sourceConfig';

export type BusySource = number | 'new' | null;

export interface SourcesState {
  items: SourceInfo[] | null;
  runs: Record<number, SourceRun[]>;
  runPages: Record<number, SourceRunPage>;
  expanded: Record<number, boolean>;
  form: SourceFormState;
  editorSession: number;
  busy: BusySource;
  armedDelete: number | null;
  healthOpenId: number | null;
  error: string | null;
}

type SourcesAction =
  | { type: 'sources-loaded'; items: SourceInfo[] }
  | { type: 'source-detail-loaded'; detail: SourceDetail }
  | { type: 'set-expanded'; id: number; open: boolean }
  | { type: 'set-adding'; adding: boolean }
  | { type: 'start-edit'; source: SourceInfo }
  | { type: 'update-form'; patch: Partial<SourceFormState> }
  | { type: 'saved-form'; session: number; form: SourceFormState; sourceId: number }
  | { type: 'form-error'; session: number; error: string }
  | { type: 'set-busy'; busy: BusySource }
  | { type: 'set-error'; error: string | null }
  | { type: 'arm-delete'; id: number | null }
  | { type: 'source-deleted'; id: number }
  | { type: 'set-health-open'; id: number | null };

export const initialSourcesState = (): SourcesState => ({
  items: null,
  runs: {},
  runPages: {},
  expanded: {},
  form: emptyForm(),
  editorSession: 0,
  busy: null,
  armedDelete: null,
  healthOpenId: null,
  error: null,
});

const sourceInfoFromDetail = (detail: SourceDetail): SourceInfo => ({
  id: detail.id,
  name: detail.name,
  kind: detail.kind,
  url: detail.url,
  config: detail.config,
  sheetId: detail.sheetId,
  schedule: detail.schedule,
  enabled: detail.enabled,
  lastCheckedAt: detail.lastCheckedAt,
  lastStatus: detail.lastStatus,
  newRowsTotal: detail.newRowsTotal,
});

export function sourcesReducer(state: SourcesState, action: SourcesAction): SourcesState {
  switch (action.type) {
    case 'sources-loaded':
      return { ...state, items: action.items };
    case 'source-detail-loaded': {
      const source = sourceInfoFromDetail(action.detail);
      return {
        ...state,
        runs: { ...state.runs, [action.detail.id]: action.detail.runs },
        runPages: { ...state.runPages, [action.detail.id]: action.detail.runsPage },
        items: state.items?.map((item) => (item.id === action.detail.id ? source : item))
          ?? state.items,
      };
    }
    case 'set-expanded':
      return { ...state, expanded: { ...state.expanded, [action.id]: action.open } };
    case 'set-adding':
      return {
        ...state,
        form: action.adding ? { ...emptyForm(), adding: true } : emptyForm(),
        editorSession: state.editorSession + 1,
        error: null,
      };
    case 'start-edit':
      return {
        ...state,
        form: sourceToForm(action.source),
        editorSession: state.editorSession + 1,
        error: null,
      };
    case 'update-form':
      return { ...state, form: { ...state.form, ...action.patch } };
    case 'saved-form':
      if (state.editorSession !== action.session) return state;
      if (state.form === action.form) return { ...state, form: emptyForm(), editorSession: state.editorSession + 1 };
      // Keep in-session edits, but subsequent saves must update the source
      // actually created rather than creating a duplicate from the retained draft.
      return action.form.editingId == null
        ? { ...state, form: { ...state.form, editingId: action.sourceId } } : state;
    case 'form-error':
      return state.editorSession === action.session ? { ...state, error: action.error } : state;
    case 'set-busy':
      return { ...state, busy: action.busy };
    case 'set-error':
      return { ...state, error: action.error };
    case 'arm-delete':
      return { ...state, armedDelete: action.id };
    case 'source-deleted': {
      const runs = { ...state.runs };
      const runPages = { ...state.runPages };
      const expanded = { ...state.expanded };
      delete runs[action.id];
      delete runPages[action.id];
      delete expanded[action.id];
      return {
        ...state,
        items: state.items?.filter((source) => source.id !== action.id) ?? state.items,
        runs,
        runPages,
        expanded,
        healthOpenId: state.healthOpenId === action.id ? null : state.healthOpenId,
      };
    }
    case 'set-health-open':
      return { ...state, healthOpenId: action.id };
    default:
      return state;
  }
}
