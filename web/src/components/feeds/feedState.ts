import type { FeedDetail, FeedInfo, FeedCheck, FeedCheckPage } from '../../api/open';
import { emptyForm, feedToForm, type FeedFormState } from './feedConfig';

export type BusyFeed = number | 'new' | null;

interface FeedsState {
  items: FeedInfo[] | null;
  runs: Record<number, FeedCheck[]>;
  runPages: Record<number, FeedCheckPage>;
  expanded: Record<number, boolean>;
  form: FeedFormState;
  editorSession: number;
  busy: BusyFeed;
  armedDelete: number | null;
  healthOpenId: number | null;
  error: string | null;
}

type FeedsAction =
  | { type: 'sources-loaded'; items: FeedInfo[] }
  | { type: 'source-detail-loaded'; detail: FeedDetail }
  | { type: 'set-expanded'; id: number; open: boolean }
  | { type: 'set-adding'; adding: boolean }
  | { type: 'start-edit'; source: FeedInfo }
  | { type: 'update-form'; patch: Partial<FeedFormState> }
  | { type: 'saved-form'; session: number; form: FeedFormState; sourceId: number }
  | { type: 'form-error'; session: number; error: string }
  | { type: 'set-busy'; busy: BusyFeed }
  | { type: 'set-error'; error: string | null }
  | { type: 'arm-delete'; id: number | null }
  | { type: 'source-deleted'; id: number }
  | { type: 'set-health-open'; id: number | null };

export const initialFeedsState = (): FeedsState => ({
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

const feedInfoFromDetail = (detail: FeedDetail): FeedInfo => ({
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

export function feedsReducer(state: FeedsState, action: FeedsAction): FeedsState {
  switch (action.type) {
    case 'sources-loaded':
      return { ...state, items: action.items };
    case 'source-detail-loaded': {
      const feed = feedInfoFromDetail(action.detail);
      return {
        ...state,
        runs: { ...state.runs, [action.detail.id]: action.detail.runs },
        runPages: { ...state.runPages, [action.detail.id]: action.detail.runsPage },
        items: state.items?.map((item) => (item.id === action.detail.id ? feed : item))
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
        form: feedToForm(action.source),
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
        items: state.items?.filter((feed) => feed.id !== action.id) ?? state.items,
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
