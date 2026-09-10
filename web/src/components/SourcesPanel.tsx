import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { PanelEmpty, PanelError } from './PanelPrimitives';
import { PanelSelect } from './PanelSelect';
import {
  Activity,
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Database,
  Eye,
  Globe2,
  ListChecks,
  ListVideo,
  Pencil,
  Plus,
  RefreshCw,
  Rss,
  Scale,
  Trash2,
  X,
} from 'lucide-react';
import type {
  SourceDetail,
  SourceConfig,
  SourceHealth,
  SourceInfo,
  SourceInput,
  SourceInterval,
  SourceKind,
  SourceRun,
  SourceRunPage,
} from '../api/open';
import { formatUsd } from '../format';
import type { SourceApiPort } from '../api/ports';
import type { WorkbenchResolvedLayoutContribution } from '../workbench/layout';
import type { PluginDetailSubject } from '../workbench/pluginDetailContext';

const EMPTY_CONTRIBUTIONS: WorkbenchResolvedLayoutContribution[] = [];

// Cadence presets - the only schedule strings the backend interval parser
// (jobs/sources.py::_schedule_interval) accepts, so the picker cannot produce an
// invalid cron. '' = manual / on-demand (a null schedule server-side).
const INTERVALS: { value: SourceInterval; label: string }[] = [
  { value: '', label: 'Manual' },
  { value: '@hourly', label: 'Hourly' },
  { value: '0 */6 * * *', label: 'Every 6 hours' },
  { value: '@daily', label: 'Daily' },
];

type KnownSourceKind =
  | 'rss'
  | 'youtube_playlist'
  | 'youtube_channel'
  | 'api_list_dicts'
  | 'courtlistener_docket';

const SOURCE_KINDS: Array<{
  value: KnownSourceKind;
  label: string;
  urlLabel: string;
  placeholder: string;
}> = [
  {
    value: 'rss',
    label: 'RSS feed',
    urlLabel: 'Feed URL',
    placeholder: 'https://example.com/feed.xml',
  },
  {
    value: 'youtube_playlist',
    label: 'YouTube playlist',
    urlLabel: 'Playlist URL or ID',
    placeholder: 'https://www.youtube.com/playlist?list=PL...',
  },
  {
    value: 'youtube_channel',
    label: 'YouTube channel',
    urlLabel: 'Channel URL, ID, or @handle',
    placeholder: 'https://www.youtube.com/@example',
  },
  {
    value: 'api_list_dicts',
    label: 'API list',
    urlLabel: 'HTTPS JSON URL',
    placeholder: 'https://example.gov/api/items',
  },
  {
    value: 'courtlistener_docket',
    label: 'CourtListener docket',
    urlLabel: 'Docket URL or ID',
    placeholder: 'https://www.courtlistener.com/docket/123456/example/',
  },
];

const SUPPORTED_KIND_VALUES = new Set<KnownSourceKind>(
  SOURCE_KINDS.map((kind) => kind.value),
);

const msg = (e: unknown): string => (e instanceof Error ? e.message : String(e));

type BusySource = number | 'new' | null;

interface SourceFormState {
  adding: boolean;
  editingId: number | null;
  name: string;
  kind: KnownSourceKind;
  url: string;
  cadence: SourceInterval;
  apiListPath: string;
  apiItemIdPath: string;
  apiUpdatedAtPath: string;
  apiMaxItems: string;
  youtubeMaxPages: string;
  youtubeMaxItems: string;
  courtKeywords: string;
  courtIncludeParties: boolean;
  courtIncludeDocuments: boolean;
  courtMaxEntries: string;
}

interface SourcesState {
  items: SourceInfo[] | null;
  runs: Record<number, SourceRun[]>;
  runPages: Record<number, SourceRunPage>;
  expanded: Record<number, boolean>;
  form: SourceFormState;
  busy: BusySource;
  armedDelete: number | null;
  healthOpenId: number | null;
  healthLoading: boolean;
  health: Record<number, SourceHealth>;
  healthError: string | null;
  error: string | null;
}

type SourcesAction =
  | { type: 'sources-loaded'; items: SourceInfo[] }
  | { type: 'source-detail-loaded'; detail: SourceDetail }
  | { type: 'set-expanded'; id: number; open: boolean }
  | { type: 'set-adding'; adding: boolean }
  | { type: 'start-edit'; source: SourceInfo }
  | { type: 'update-form'; patch: Partial<SourceFormState> }
  | { type: 'reset-form' }
  | { type: 'set-busy'; busy: BusySource }
  | { type: 'set-error'; error: string | null }
  | { type: 'arm-delete'; id: number | null }
  | { type: 'source-deleted'; id: number }
  | { type: 'set-health-open'; id: number | null }
  | { type: 'set-health-loading'; loading: boolean }
  | { type: 'health-loaded'; health: SourceHealth }
  | { type: 'set-health-error'; error: string | null };

const emptyForm = (): SourceFormState => ({
  adding: false,
  editingId: null,
  name: '',
  kind: 'rss',
  url: '',
  cadence: '@hourly',
  apiListPath: '/',
  apiItemIdPath: '',
  apiUpdatedAtPath: '',
  apiMaxItems: '',
  youtubeMaxPages: '',
  youtubeMaxItems: '',
  courtKeywords: '',
  courtIncludeParties: true,
  courtIncludeDocuments: true,
  courtMaxEntries: '',
});

const initialSourcesState = (): SourcesState => ({
  items: null,
  runs: {},
  runPages: {},
  expanded: {},
  form: emptyForm(),
  busy: null,
  armedDelete: null,
  healthOpenId: null,
  healthLoading: false,
  health: {},
  healthError: null,
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

function sourcesReducer(state: SourcesState, action: SourcesAction): SourcesState {
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
        error: null,
      };
    case 'start-edit':
      return {
        ...state,
        form: sourceToForm(action.source),
        error: null,
      };
    case 'update-form':
      return { ...state, form: { ...state.form, ...action.patch } };
    case 'reset-form':
      return { ...state, form: emptyForm() };
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
      const health = { ...state.health };
      delete runs[action.id];
      delete runPages[action.id];
      delete expanded[action.id];
      delete health[action.id];
      return {
        ...state,
        items: state.items?.filter((source) => source.id !== action.id) ?? state.items,
        runs,
        runPages,
        expanded,
        health,
        healthOpenId: state.healthOpenId === action.id ? null : state.healthOpenId,
      };
    }
    case 'set-health-open':
      return { ...state, healthOpenId: action.id, healthError: null };
    case 'set-health-loading':
      return { ...state, healthLoading: action.loading };
    case 'health-loaded':
      return {
        ...state,
        health: { ...state.health, [action.health.source.id]: action.health },
        healthLoading: false,
        healthError: null,
      };
    case 'set-health-error':
      return { ...state, healthLoading: false, healthError: action.error };
    default:
      return state;
  }
}

function normalizeKind(kind: SourceKind): KnownSourceKind {
  return SUPPORTED_KIND_VALUES.has(kind as KnownSourceKind)
    ? kind as KnownSourceKind
    : 'rss';
}

function configString(config: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = config[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  }
  return '';
}

function configNumber(config: Record<string, unknown>, key: string): string {
  const value = config[key];
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : '';
}

function configBoolean(
  config: Record<string, unknown>,
  key: string,
  fallback: boolean,
): boolean {
  const value = config[key];
  return typeof value === 'boolean' ? value : fallback;
}

function sourceConfigObject(config: SourceConfig) {
  return typeof config === 'object' && config !== null && !Array.isArray(config)
    ? config
    : {};
}

function urlHasSensitiveParts(value: string | null | undefined): boolean {
  if (!value) return false;
  try {
    const parsed = new URL(value);
    return Boolean(parsed.username || parsed.password || parsed.search || parsed.hash);
  } catch {
    return false;
  }
}

function redactedUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    if (!parsed.protocol || !parsed.hostname) return '[redacted]';
    return `${parsed.protocol}//${parsed.host}${parsed.pathname}`;
  } catch {
    return value.length > 80 ? `${value.slice(0, 77)}...` : value;
  }
}

function editableUrl(value: string | null | undefined): string {
  if (!value || urlHasSensitiveParts(value)) return '';
  return value;
}

function sourceToForm(source: SourceInfo): SourceFormState {
  const kind = normalizeKind(source.kind);
  const config = sourceConfigObject(source.config);
  const keywords = Array.isArray(config.keywords)
    ? config.keywords.filter((item): item is string => typeof item === 'string').join(', ')
    : '';
  const sourceRef = source.url
    ?? configString(config, ['playlist_id', 'playlist_url', 'channel_handle', 'channel_id', 'channel_url', 'docket_url', 'docket_id', 'url']);
  const visibleRef = kind === 'rss' && urlHasSensitiveParts(sourceRef) ? '' : (sourceRef ?? '');
  return {
    ...emptyForm(),
    adding: true,
    editingId: source.id,
    name: source.name,
    kind,
    url: kind === 'rss' ? editableUrl(visibleRef) : visibleRef,
    cadence: (source.schedule ?? '') as SourceInterval,
    apiListPath: configString(config, ['list_path']) || '/',
    apiItemIdPath: configString(config, ['item_id_path']),
    apiUpdatedAtPath: configString(config, ['updated_at_path']),
    apiMaxItems: configNumber(config, 'max_items'),
    youtubeMaxPages: configNumber(config, 'max_pages_per_poll'),
    youtubeMaxItems: configNumber(config, 'max_items_per_poll'),
    courtKeywords: keywords,
    courtIncludeParties: configBoolean(config, 'include_parties', true),
    courtIncludeDocuments: configBoolean(config, 'include_documents', true),
    courtMaxEntries: configNumber(config, 'max_entries_per_poll'),
  };
}

function splitKeywords(value: string): string[] {
  return value.split(/[\n,]/).flatMap((item) => {
    const trimmed = item.trim();
    return trimmed ? [trimmed] : [];
  });
}

function optionalPositiveInt(value: string, field: string): number | undefined {
  if (!value.trim()) return undefined;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`${field} must be a positive whole number`);
  }
  return parsed;
}

function apiUrlIsHttps(value: string): boolean {
  try {
    return new URL(value).protocol === 'https:';
  } catch {
    return false;
  }
}

function buildSourceInput(form: SourceFormState): SourceInput {
  const name = form.name.trim();
  const url = form.url.trim();
  if (!name) throw new Error('Source name is required');
  if (!url) throw new Error(`${sourceKindLabel(form.kind)} requires a source reference`);

  const base: SourceInput = {
    name,
    kind: form.kind,
    url,
    schedule: form.cadence || null,
    enabled: true,
    config: {},
  };

  switch (form.kind) {
    case 'rss':
      return {
        ...base,
        config: { merge_strategy: 'append-new' },
      };
    case 'youtube_playlist': {
      const config: Record<string, unknown> = {
        schema_version: 'frisket.source.youtube.v1',
        playlist_url: url,
      };
      const maxPages = optionalPositiveInt(form.youtubeMaxPages, 'Max pages');
      const maxItems = optionalPositiveInt(form.youtubeMaxItems, 'Max items');
      if (maxPages !== undefined) config.max_pages_per_poll = maxPages;
      if (maxItems !== undefined) config.max_items_per_poll = maxItems;
      return { ...base, config };
    }
    case 'youtube_channel': {
      const config: Record<string, unknown> = {
        schema_version: 'frisket.source.youtube.v1',
        channel_url: url,
      };
      const maxPages = optionalPositiveInt(form.youtubeMaxPages, 'Max pages');
      const maxItems = optionalPositiveInt(form.youtubeMaxItems, 'Max items');
      if (maxPages !== undefined) config.max_pages_per_poll = maxPages;
      if (maxItems !== undefined) config.max_items_per_poll = maxItems;
      return { ...base, config };
    }
    case 'api_list_dicts': {
      if (!apiUrlIsHttps(url)) throw new Error('API list sources require an HTTPS URL');
      const config: Record<string, unknown> = {
        schema_version: 'frisket.source.api_list_dicts.v1',
        method: 'GET',
        url,
        list_path: form.apiListPath.trim() || '/',
        schema_policy: 'additive',
      };
      if (form.apiItemIdPath.trim()) config.item_id_path = form.apiItemIdPath.trim();
      if (form.apiUpdatedAtPath.trim()) config.updated_at_path = form.apiUpdatedAtPath.trim();
      const maxItems = optionalPositiveInt(form.apiMaxItems, 'Max items');
      if (maxItems !== undefined) config.max_items = maxItems;
      return { ...base, config };
    }
    case 'courtlistener_docket': {
      const config: Record<string, unknown> = {
        schema_version: 'frisket.source.courtlistener_docket.v1',
        keywords: splitKeywords(form.courtKeywords),
        include_parties: form.courtIncludeParties,
        include_documents: form.courtIncludeDocuments,
        recap_pdf_policy: 'link_only',
      };
      if (/^[1-9][0-9]*$/.test(url)) {
        config.docket_id = Number(url);
        base.url = null;
      } else {
        config.docket_url = url;
      }
      const maxEntries = optionalPositiveInt(form.courtMaxEntries, 'Max entries');
      if (maxEntries !== undefined) config.max_entries_per_poll = maxEntries;
      return { ...base, config };
    }
  }
}

function sourceKindLabel(kind: SourceKind): string {
  switch (kind) {
    case 'rss':
      return 'RSS feed';
    case 'youtube_playlist':
      return 'YouTube playlist';
    case 'youtube_channel':
      return 'YouTube channel';
    case 'api_list_dicts':
      return 'API list';
    case 'courtlistener_docket':
      return 'CourtListener docket';
    default:
      return String(kind).replaceAll('_', ' ');
  }
}

function sourceKindIcon(kind: SourceKind) {
  switch (kind) {
    case 'youtube_playlist':
    case 'youtube_channel':
      return <ListVideo size={12} aria-hidden />;
    case 'api_list_dicts':
      return <Globe2 size={12} aria-hidden />;
    case 'courtlistener_docket':
      return <Scale size={12} aria-hidden />;
    default:
      return <Rss size={12} aria-hidden />;
  }
}

function sourceConfigSummary(source: SourceInfo): string {
  const config = sourceConfigObject(source.config);
  switch (source.kind) {
    case 'youtube_playlist':
      return `playlist ${configString(config, ['playlist_id', 'playlist_url']) || redactedUrl(source.url) || ''}`.trim();
    case 'youtube_channel':
      return `channel ${configString(config, ['channel_handle', 'channel_id', 'channel_url']) || redactedUrl(source.url) || ''}`.trim();
    case 'api_list_dicts':
      return `list path ${configString(config, ['list_path']) || '/'}`;
    case 'courtlistener_docket': {
      const docket = configString(config, ['docket_id', 'docket_url']) || redactedUrl(source.url) || '';
      const keywords = Array.isArray(config.keywords) && config.keywords.length > 0
        ? `, ${config.keywords.length} terms`
        : '';
      return `docket ${docket}${keywords}`;
    }
    default:
      return 'append-new';
  }
}

function statusLabel(value: string | null | undefined): string {
  if (!value || value === 'never') return 'never run';
  return value.replaceAll('_', ' ');
}

function statusClass(value: string | null | undefined): string {
  return `status-${value || 'never'}`.replace(/[^a-z0-9_-]/gi, '-');
}

function formatWhen(value: string | null | undefined): string {
  if (!value) return 'never';
  return new Date(value).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function formatDuration(ms: number | null): string {
  if (ms == null) return '';
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function formatCost(micro: number): string {
  return formatUsd(micro / 1_000_000);
}

function runDeltaText(run: Pick<SourceRun, 'newRows' | 'changedRows' | 'skippedRows' | 'revisions'>): string {
  const parts: string[] = [];
  if (run.newRows) parts.push(`+${run.newRows} new`);
  if (run.changedRows) parts.push(`${run.changedRows} changed`);
  if (run.revisions) parts.push(`${run.revisions} revised`);
  if (run.skippedRows) parts.push(`${run.skippedRows} skipped`);
  return parts.join(' / ') || '+0 new';
}

function selectedKind(form: SourceFormState) {
  return SOURCE_KINDS.find((kind) => kind.value === form.kind) ?? SOURCE_KINDS[0];
}

function useSourcesPanelState(
  sourceApi: SourceApiPort,
  onPolled?: (sheetId: number | null) => void,
) {
  const [state, dispatch] = useReducer(sourcesReducer, undefined, initialSourcesState);

  const refresh = useCallback(async () => {
    try {
      const items = await sourceApi.listSources();
      dispatch({ type: 'sources-loaded', items });
    } catch (e) {
      dispatch({ type: 'set-error', error: msg(e) });
    }
  }, [sourceApi]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const onChanged = () => {
      void refresh();
    };
    window.addEventListener('frisket:sources-changed', onChanged);
    return () => window.removeEventListener('frisket:sources-changed', onChanged);
  }, [refresh]);

  const loadRuns = useCallback(async (id: number, reportError = true) => {
    try {
      const detail = await sourceApi.getSource(id, 0, 20);
      dispatch({ type: 'source-detail-loaded', detail });
    } catch (e) {
      if (reportError) dispatch({ type: 'set-error', error: msg(e) });
    }
  }, [sourceApi]);

  const loadHealth = useCallback(async (id: number, reportError = true) => {
    dispatch({ type: 'set-health-loading', loading: true });
    try {
      const health = await sourceApi.getSourceHealth(id, 0, 20);
      dispatch({ type: 'health-loaded', health });
    } catch (e) {
      if (reportError) dispatch({ type: 'set-health-error', error: msg(e) });
      else dispatch({ type: 'set-health-loading', loading: false });
    }
  }, [sourceApi]);

  const submit = useCallback(async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    let input: SourceInput;
    try {
      input = buildSourceInput(state.form);
    } catch (error) {
      dispatch({ type: 'set-error', error: msg(error) });
      return;
    }

    const editingId = state.form.editingId;
    dispatch({ type: 'set-busy', busy: editingId ?? 'new' });
    dispatch({ type: 'set-error', error: null });
    try {
      if (editingId == null) {
        await sourceApi.createSource(input);
      } else {
        await sourceApi.updateSource(editingId, input);
      }
      dispatch({ type: 'reset-form' });
      await refresh();
      if (editingId != null && state.healthOpenId === editingId) {
        await loadHealth(editingId, false);
      }
    } catch (error) {
      dispatch({ type: 'set-error', error: msg(error) });
    } finally {
      dispatch({ type: 'set-busy', busy: null });
    }
  }, [loadHealth, refresh, sourceApi, state.form, state.healthOpenId]);

  const fetchNow = useCallback(async (id: number) => {
    dispatch({ type: 'set-busy', busy: id });
    dispatch({ type: 'set-error', error: null });
    try {
      const res = await sourceApi.fetchSource(id);
      onPolled?.(res.sheetId);
    } catch (e) {
      // A failed poll is recorded server-side; surface it inline and let the
      // run history / health drawer show the detail from the redacted routes.
      dispatch({ type: 'set-error', error: `Poll failed: ${msg(e)}` });
    } finally {
      dispatch({ type: 'set-busy', busy: null });
      dispatch({ type: 'set-expanded', id, open: true });
      await loadRuns(id, false);
      if (state.healthOpenId === id) await loadHealth(id, false);
    }
  }, [loadHealth, loadRuns, onPolled, sourceApi, state.healthOpenId]);

  const changeSchedule = useCallback(async (id: number, value: string) => {
    dispatch({ type: 'set-error', error: null });
    try {
      await sourceApi.updateSource(id, { schedule: value || null });
      await refresh();
      if (state.healthOpenId === id) await loadHealth(id, false);
    } catch (e) {
      dispatch({ type: 'set-error', error: msg(e) });
    }
  }, [loadHealth, refresh, sourceApi, state.healthOpenId]);

  const toggleEnabled = useCallback(async (id: number, enabled: boolean) => {
    dispatch({ type: 'set-error', error: null });
    try {
      await sourceApi.updateSource(id, { enabled });
      await refresh();
      if (state.healthOpenId === id) await loadHealth(id, false);
    } catch (e) {
      dispatch({ type: 'set-error', error: msg(e) });
    }
  }, [loadHealth, refresh, sourceApi, state.healthOpenId]);

  const remove = useCallback(async (id: number) => {
    if (state.armedDelete !== id) {
      dispatch({ type: 'arm-delete', id });
      return;
    }
    dispatch({ type: 'arm-delete', id: null });
    try {
      await sourceApi.deleteSource(id);
      dispatch({ type: 'source-deleted', id });
    } catch (e) {
      dispatch({ type: 'set-error', error: msg(e) });
    }
  }, [sourceApi, state.armedDelete]);

  const toggleRuns = useCallback(async (id: number) => {
    const open = !state.expanded[id];
    dispatch({ type: 'set-expanded', id, open });
    if (open && !state.runs[id]) await loadRuns(id);
  }, [loadRuns, state.expanded, state.runs]);

  const openHealth = useCallback(async (id: number) => {
    const nextId = state.healthOpenId === id ? null : id;
    dispatch({ type: 'set-health-open', id: nextId });
    if (nextId != null) await loadHealth(nextId);
  }, [loadHealth, state.healthOpenId]);

  return {
    state,
    actions: {
      setAdding: (adding: boolean) => dispatch({ type: 'set-adding', adding }),
      startEdit: (source: SourceInfo) => dispatch({ type: 'start-edit', source }),
      updateForm: (patch: Partial<SourceFormState>) => dispatch({ type: 'update-form', patch }),
      submit,
      fetchNow,
      changeSchedule,
      toggleEnabled,
      remove,
      toggleRuns,
      openHealth,
      closeHealth: () => dispatch({ type: 'set-health-open', id: null }),
    },
  };
}

export interface SourcesPanelProps {
  sourceApi: SourceApiPort;
  /** Fired after a manual poll. sheetId is the source sheet, so the workspace can refresh/navigate. */
  onPolled?(sheetId: number | null): void;
  onOpenSourceHealthMainView?(sourceId: number): void;
  /** Opens the EXISTING Import workspace dialog
   *  (its "Feed" mode owns source creation — sources.spec.ts pins that
   *  SourcesPanel must not own a second creation form). Omitted -> no Add
   *  affordance renders, rather than a dead button. */
  onOpenImportDialog?(): void;
  sourceKindFormFrame?: SourceKindFormFrame;
  sourceDetailFrames?: SourceDetailFrames;
  /** Resolved sourceDetail contributions + the workspace dispatch for plugin
   *  detail tabs. */
  sourceDetailContributions?: WorkbenchResolvedLayoutContribution[];
  renderPluginDetailTab?(
    contribution: WorkbenchResolvedLayoutContribution,
    subject: PluginDetailSubject,
  ): ReactNode;
}

type SourcePanelActions = ReturnType<typeof useSourcesPanelState>['actions'];

// First-party source detail tab bindings: legacy short names (pinned testids)
// + which contribution frame wraps each tab button. The tab SET renders from
// resolved sourceDetail contributions — no hardcoded tab union.
const FIRST_PARTY_SOURCE_DETAIL_TABS: Record<
  string,
  { shortName: 'summary' | 'health' | 'runs'; frameKey: keyof SourceDetailFrames }
> = {
  'frisket.core.view.source_summary': { shortName: 'summary', frameKey: 'Summary' },
  'frisket.core.view.source_health': { shortName: 'health', frameKey: 'Health' },
  'frisket.core.view.source_runs': { shortName: 'runs', frameKey: 'Runs' },
};
type SourceDetailFrame = ({ children }: { children: ReactNode }) => ReactNode;
type SourceKindFormFrame = ({ sourceKind, children }: {
  sourceKind: KnownSourceKind;
  children: ReactNode;
}) => ReactNode;

export interface SourceDetailFrames {
  Summary: SourceDetailFrame;
  Health: SourceDetailFrame;
  Runs: SourceDetailFrame;
}

const PlainSourceDetailFrame: SourceDetailFrame = ({ children }) => <>{children}</>;
const PLAIN_SOURCE_DETAIL_FRAMES: SourceDetailFrames = {
  Summary: PlainSourceDetailFrame,
  Health: PlainSourceDetailFrame,
  Runs: PlainSourceDetailFrame,
};

type SourceHealthMainState = {
  health: SourceHealth | null;
  loading: boolean;
  error: string | null;
  busy: boolean;
};

type SourceHealthMainAction =
  | { type: 'loadStart' }
  | { type: 'loadSuccess'; health: SourceHealth }
  | { type: 'loadFailure'; message: string }
  | { type: 'fetchStart' }
  | { type: 'fetchFailure'; message: string }
  | { type: 'fetchSettled' };

const initialSourceHealthMainState = (): SourceHealthMainState => ({
  health: null,
  loading: true,
  error: null,
  busy: false,
});

function sourceHealthMainReducer(
  state: SourceHealthMainState,
  action: SourceHealthMainAction,
): SourceHealthMainState {
  switch (action.type) {
    case 'loadStart':
      return { ...state, loading: true, error: null };
    case 'loadSuccess':
      return { ...state, health: action.health, loading: false, error: null };
    case 'loadFailure':
      return { ...state, loading: false, error: action.message };
    case 'fetchStart':
      return { ...state, busy: true, error: null };
    case 'fetchFailure':
      return { ...state, error: action.message };
    case 'fetchSettled':
      return { ...state, busy: false };
    default:
      return state;
  }
}

interface SourceEditorFormProps {
  actions: SourcePanelActions;
  busy: BusySource;
  form: SourceFormState;
  formFrame?: SourceKindFormFrame;
}

function SourceEditorForm({ actions, busy, form, formFrame }: SourceEditorFormProps) {
  const kind = selectedKind(form);
  const Frame = formFrame;

  const formElement = (
    <form className="sources-form" data-testid="source-form" onSubmit={actions.submit}>
      <div className="sources-form-row">
        <PanelSelect
          className="form-input"
          aria-label="Source type"
          data-testid="source-kind"
          value={form.kind}
          onChange={(e) => actions.updateForm({ kind: e.target.value as KnownSourceKind })}
        >
          {SOURCE_KINDS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </PanelSelect>
      </div>
      <input
        className="form-input"
        placeholder="Source name"
        aria-label="Source name"
        data-testid="source-name"
        value={form.name}
        onChange={(e) => actions.updateForm({ name: e.target.value })}
      />
      <label className="source-field-label" htmlFor="source-url-input">
        {kind.urlLabel}
      </label>
      <input
        id="source-url-input"
        className="form-input"
        placeholder={kind.placeholder}
        aria-label={kind.urlLabel}
        data-testid="source-url"
        value={form.url}
        onChange={(e) => actions.updateForm({ url: e.target.value })}
      />

      {(form.kind === 'youtube_playlist' || form.kind === 'youtube_channel') && (
        <div className="sources-form-grid">
          <label>
            <span>Max pages</span>
            <input
              className="form-input"
              aria-label="YouTube max pages"
              data-testid="source-youtube-max-pages"
              value={form.youtubeMaxPages}
              onChange={(e) => actions.updateForm({ youtubeMaxPages: e.target.value })}
            />
          </label>
          <label>
            <span>Max items</span>
            <input
              className="form-input"
              aria-label="YouTube max items"
              data-testid="source-youtube-max-items"
              value={form.youtubeMaxItems}
              onChange={(e) => actions.updateForm({ youtubeMaxItems: e.target.value })}
            />
          </label>
        </div>
      )}

      {form.kind === 'api_list_dicts' && (
        <>
          <div className="sources-form-grid">
            <label>
              <span>List path</span>
              <input
                className="form-input"
                aria-label="API list path"
                data-testid="source-api-list-path"
                value={form.apiListPath}
                onChange={(e) => actions.updateForm({ apiListPath: e.target.value })}
              />
            </label>
            <label>
              <span>Item ID path</span>
              <input
                className="form-input"
                aria-label="API item ID path"
                data-testid="source-api-item-id-path"
                value={form.apiItemIdPath}
                onChange={(e) => actions.updateForm({ apiItemIdPath: e.target.value })}
              />
            </label>
          </div>
          <div className="sources-form-grid">
            <label>
              <span>Updated path</span>
              <input
                className="form-input"
                aria-label="API updated-at path"
                data-testid="source-api-updated-at-path"
                value={form.apiUpdatedAtPath}
                onChange={(e) => actions.updateForm({ apiUpdatedAtPath: e.target.value })}
              />
            </label>
            <label>
              <span>Max items</span>
              <input
                className="form-input"
                aria-label="API max items"
                data-testid="source-api-max-items"
                value={form.apiMaxItems}
                onChange={(e) => actions.updateForm({ apiMaxItems: e.target.value })}
              />
            </label>
          </div>
        </>
      )}

      {form.kind === 'courtlistener_docket' && (
        <>
          <textarea
            className="form-input source-textarea"
            aria-label="CourtListener keywords"
            data-testid="source-court-keywords"
            value={form.courtKeywords}
            onChange={(e) => actions.updateForm({ courtKeywords: e.target.value })}
            placeholder="injunction, settlement"
          />
          <div className="source-check-row">
            <label>
              <input
                type="checkbox"
                data-testid="source-court-include-parties"
                checked={form.courtIncludeParties}
                onChange={(e) => actions.updateForm({ courtIncludeParties: e.target.checked })}
              />
              Parties
            </label>
            <label>
              <input
                type="checkbox"
                data-testid="source-court-include-documents"
                checked={form.courtIncludeDocuments}
                onChange={(e) => actions.updateForm({ courtIncludeDocuments: e.target.checked })}
              />
              Documents
            </label>
          </div>
          <label className="source-field-label">
            Max entries
            <input
              className="form-input"
              aria-label="CourtListener max entries"
              data-testid="source-court-max-entries"
              value={form.courtMaxEntries}
              onChange={(e) => actions.updateForm({ courtMaxEntries: e.target.value })}
            />
          </label>
          <div className="source-note">RECAP PDFs stay link-only. Runtime tokens are not stored in source config.</div>
        </>
      )}

      <PanelSelect
        className="form-input"
        aria-label="Polling cadence"
        data-testid="source-interval"
        value={form.cadence}
        onChange={(e) => actions.updateForm({ cadence: e.target.value as SourceInterval })}
      >
        {INTERVALS.map((i) => (
          <option key={i.value} value={i.value}>{i.label}</option>
        ))}
      </PanelSelect>
      <div className="sources-form-actions">
        <button
          type="button"
          className="mini-btn"
          onClick={() => actions.setAdding(false)}
        >
          Cancel
        </button>
        <button
          type="submit"
          className="btn btn-primary mini-btn"
          data-testid="source-create"
          disabled={busy !== null || !form.name.trim() || !form.url.trim()}
        >
          {busy !== null
            ? (form.editingId == null ? 'Adding...' : 'Saving...')
            : (form.editingId == null ? 'Add' : 'Save')}
        </button>
      </div>
    </form>
  );

  if (!Frame) return formElement;
  return <Frame sourceKind={form.kind}>{formElement}</Frame>;
}

interface SourceListProps {
  actions: SourcePanelActions;
  armedDelete: number | null;
  busy: BusySource;
  expanded: Record<number, boolean>;
  items: SourceInfo[] | null;
  runPages: Record<number, SourceRunPage>;
  runs: Record<number, SourceRun[]>;
}

function SourceList({ actions, armedDelete, busy, expanded, items, runPages, runs }: SourceListProps) {
  if (items === null) return <PanelEmpty>loading...</PanelEmpty>;
  if (items.length === 0) {
    return (
      <PanelEmpty testId="sources-empty">No live sources yet.</PanelEmpty>
    );
  }

  return (
    <ul className="sources-list" data-testid="source-list">
      {items.map((s) => {
        const status = s.lastStatus ?? 'never';
        const isOpen = !!expanded[s.id];
        const page = runPages[s.id];
        const RunChevron = isOpen ? ChevronDown : ChevronRight;
        return (
          <li
            key={s.id}
            className={`source-item${s.enabled ? '' : ' disabled'}`}
            data-testid={`source-item-${s.id}`}
          >
            <div className="source-head">
              <span className="source-kind-icon">{sourceKindIcon(s.kind)}</span>
              <span className="source-name" title={s.name}>{s.name}</span>
              <span
                className={`source-status ${statusClass(status)}`}
                data-testid={`source-status-${s.id}`}
              >
                {statusLabel(status)}
              </span>
            </div>
            <div className="source-kind-line">
              <span data-testid={`source-kind-${s.id}`}>{sourceKindLabel(s.kind)}</span>
              <span>{sourceConfigSummary(s)}</span>
            </div>
            {s.url && (
              <div className="source-url" title={redactedUrl(s.url) ?? ''}>
                {redactedUrl(s.url)}
              </div>
            )}

            <div className="source-controls">
              <label className="source-enabled" title="Enable scheduled polling">
                <input
                  type="checkbox"
                  checked={s.enabled}
                  aria-label={`Enable ${s.name}`}
                  onChange={(e) => { void actions.toggleEnabled(s.id, e.target.checked); }}
                />
              </label>
              <PanelSelect
                className="form-input source-sched"
                aria-label={`Cadence for ${s.name}`}
                data-testid={`source-sched-${s.id}`}
                value={s.schedule ?? ''}
                onChange={(e) => { void actions.changeSchedule(s.id, e.target.value); }}
              >
                {INTERVALS.map((i) => (
                  <option key={i.value} value={i.value}>{i.label}</option>
                ))}
                {s.schedule && !INTERVALS.some((i) => i.value === s.schedule) && (
                  <option value={s.schedule}>{s.schedule}</option>
                )}
              </PanelSelect>
              <button
                type="button"
                className="icon-btn"
                title="Poll now"
                aria-label={`Poll ${s.name}`}
                data-testid={`source-fetch-${s.id}`}
                disabled={busy === s.id}
                onClick={() => { void actions.fetchNow(s.id); }}
              >
                <RefreshCw size={13} className={busy === s.id ? 'spin' : undefined} />
              </button>
              <button
                type="button"
                className="icon-btn"
                title="Edit source"
                aria-label={`Edit ${s.name}`}
                data-testid={`source-edit-${s.id}`}
                onClick={() => actions.startEdit(s)}
              >
                <Pencil size={13} />
              </button>
              <button
                type="button"
                className="icon-btn"
                title="Source details"
                aria-label={`Source details for ${s.name}`}
                data-testid={`source-health-${s.id}`}
                onClick={() => { void actions.openHealth(s.id); }}
              >
                <Eye size={13} />
              </button>
              <button
                type="button"
                className={`icon-btn${armedDelete === s.id ? ' danger' : ''}`}
                title={armedDelete === s.id ? 'Click again to confirm' : 'Delete source'}
                aria-label={
                  armedDelete === s.id
                    ? `Confirm delete ${s.name}`
                    : `Delete ${s.name}`
                }
                data-testid={`source-delete-${s.id}`}
                onClick={() => { void actions.remove(s.id); }}
              >
                {armedDelete === s.id ? <X size={13} /> : <Trash2 size={13} />}
              </button>
            </div>

            <div className="source-meta">
              {s.newRowsTotal > 0 && <>{s.newRowsTotal.toLocaleString()} rows / </>}
              <button
                type="button"
                className="source-runs-toggle"
                aria-expanded={isOpen}
                onClick={() => { void actions.toggleRuns(s.id); }}
              >
                <RunChevron size={11} aria-hidden /> poll history
              </button>
            </div>

            {isOpen && (
              <ul className="source-runs" data-testid={`source-runs-${s.id}`}>
                {runs[s.id] === undefined ? (
                  <li className="panel-empty">loading...</li>
                ) : runs[s.id].length === 0 ? (
                  <li className="panel-empty">No polls yet.</li>
                ) : (
                  <>
                    {runs[s.id].map((r) => (
                      <li key={r.id} className={`source-run ${statusClass(r.status)}`}>
                        <span className={`source-status ${statusClass(r.status)}`}>{statusLabel(r.status)}</span>
                        <span className="source-run-rows">{runDeltaText(r)}</span>
                        <span className="source-run-when">{formatWhen(r.startedAt)}</span>
                        {r.warningCount > 0 && (
                          <span className="source-run-warning">
                            {r.warningCount} warning{r.warningCount === 1 ? '' : 's'}
                          </span>
                        )}
                        {r.error && <span className="source-run-error">{r.error}</span>}
                      </li>
                    ))}
                    {page?.hasMore && (
                      <li className="panel-empty" data-testid={`source-runs-page-note-${s.id}`}>
                        {page.total.toLocaleString()} total polls / older polls not loaded
                      </li>
                    )}
                  </>
                )}
              </ul>
            )}
          </li>
        );
      })}
    </ul>
  );
}

interface SourceHealthDrawerProps {
  actions: SourcePanelActions;
  busy: BusySource;
  healthError: string | null;
  healthLoading: boolean;
  healthOpenId: number | null;
  healthSource: SourceInfo | null;
  openHealth: SourceHealth | null;
  onOpenSourceHealthMainView?: (sourceId: number) => void;
  sourceDetailFrames?: SourceDetailFrames;
  sourceDetailContributions?: WorkbenchResolvedLayoutContribution[];
  renderPluginDetailTab?: (
    contribution: WorkbenchResolvedLayoutContribution,
    subject: PluginDetailSubject,
  ) => ReactNode;
}

function SourceHealthDrawer({
  actions,
  busy,
  healthError,
  healthLoading,
  healthOpenId,
  healthSource,
  openHealth,
  onOpenSourceHealthMainView,
  sourceDetailFrames,
  sourceDetailContributions = EMPTY_CONTRIBUTIONS,
  renderPluginDetailTab,
}: SourceHealthDrawerProps) {
  // Active tab is the SHORT NAME for first-party tabs (pinned testids/content
  // dispatch) or the contributionId for plugin tabs.
  const [tabState, setTabState] = useState<{ sourceId: number | null; tab: string }>({
    sourceId: null,
    tab: 'summary',
  });
  const Frames = sourceDetailFrames ?? PLAIN_SOURCE_DETAIL_FRAMES;

  if (healthOpenId == null) return null;

  const detailTabs = sourceDetailContributions.filter(
    (contribution) =>
      contribution.mode === 'tab' &&
      (contribution.status === 'enabled' || contribution.status === 'disabled'),
  );
  const activeTab = tabState.sourceId === healthOpenId ? tabState.tab : 'summary';
  const setActiveTab = (tab: string) => setTabState({ sourceId: healthOpenId, tab });
  const subject: PluginDetailSubject = { kind: 'source', sourceId: String(healthOpenId) };
  const activePluginTab = detailTabs.find(
    (contribution) =>
      contribution.runtimeSource === 'runtimeIndex' &&
      contribution.contributionId === activeTab,
  );

  return (
    <aside className="source-health-drawer" data-testid="source-health-drawer">
      <div className="source-health-head">
        <div>
          <div className="source-health-title">{healthSource?.name ?? 'Source details'}</div>
          {healthSource && <div className="source-health-subtitle">{sourceKindLabel(healthSource.kind)}</div>}
        </div>
        {onOpenSourceHealthMainView && (
          <button
            type="button"
            className="mini-btn"
            data-testid={`source-health-open-mainView-${healthOpenId}`}
            onClick={() => {
              onOpenSourceHealthMainView(healthOpenId);
              actions.closeHealth();
            }}
          >
            Open main view
          </button>
        )}
        <button
          type="button"
          className="icon-btn"
          aria-label="Close source details"
          onClick={actions.closeHealth}
        >
          <X size={14} />
        </button>
      </div>

      <div className="source-detail-tabstrip" role="tablist" aria-label="Source detail">
        {detailTabs.map((tab) => {
          const firstParty = FIRST_PARTY_SOURCE_DETAIL_TABS[tab.contributionId];
          const tabKey = firstParty?.shortName ?? tab.contributionId;
          const testId = firstParty
            ? `source-detail-tab-${firstParty.shortName}`
            : `source-detail-tab-${tab.contributionId.replace(/[^a-zA-Z0-9]+/g, '-')}`;
          const button = (
            <button
              key={tab.placementId}
              type="button"
              role="tab"
              className="source-detail-tab"
              data-testid={testId}
              data-contribution-id={tab.contributionId}
              data-runtime-source={tab.runtimeSource}
              aria-selected={activeTab === tabKey}
              onClick={() => setActiveTab(tabKey)}
            >
              {tab.shortTitle ?? tab.title}
            </button>
          );
          if (!firstParty) return button;
          const Frame = Frames[firstParty.frameKey];
          return <Frame key={tab.placementId}>{button}</Frame>;
        })}
      </div>
      {activePluginTab && renderPluginDetailTab
        ? renderPluginDetailTab(activePluginTab, subject)
        : null}

      {healthError && (
        <PanelError testId="source-health-error">{healthError}</PanelError>
      )}
      {healthLoading && !openHealth ? (
        <PanelEmpty>loading...</PanelEmpty>
      ) : openHealth ? (
        <>
          {activeTab === 'summary' && (
            <div className="source-detail-panel" data-testid="source-detail-summary-panel">
              <strong>{openHealth.source.name}</strong>
              <span>{sourceKindLabel(openHealth.source.kind)}</span>
              <span>{healthSource ? sourceConfigSummary(healthSource) : redactedUrl(openHealth.source.url) ?? 'source details'}</span>
              <span>
                {openHealth.source.enabled ? 'enabled' : 'disabled'} /{' '}
                <span data-testid={`source-health-status-${healthOpenId}`}>
                  {statusLabel(openHealth.summary.status)}
                </span>
              </span>
            </div>
          )}

          {activeTab === 'health' && (
            <>
              <div className="source-health-summary" data-testid={`source-health-summary-${healthOpenId}`}>
                <div>
                  <span className="source-health-label">Status</span>
                  <span
                    className={`source-status ${statusClass(openHealth.summary.status)}`}
                    data-testid={`source-health-status-${healthOpenId}`}
                  >
                    {statusLabel(openHealth.summary.status)}
                  </span>
                </div>
                <div>
                  <span className="source-health-label">Last success</span>
                  <strong data-testid={`source-health-last-success-${healthOpenId}`}>
                    {formatWhen(openHealth.summary.lastSuccessAt)}
                  </strong>
                </div>
                <div>
                  <span className="source-health-label">Last failure</span>
                  <strong data-testid={`source-health-last-failure-${healthOpenId}`}>
                    {formatWhen(openHealth.summary.lastFailureAt)}
                  </strong>
                </div>
                <div>
                  <span className="source-health-label">Failures</span>
                  <strong data-testid={`source-health-consecutive-failures-${healthOpenId}`}>
                    {openHealth.summary.consecutiveFailures}
                  </strong>
                </div>
              </div>

              <div className="source-health-deltas" data-testid={`source-health-deltas-${healthOpenId}`}>
                <span>+{openHealth.summary.newRowsRecent} new</span>
                <span>{openHealth.summary.changedRowsRecent} changed</span>
                <span>{openHealth.summary.skippedRowsRecent} skipped</span>
                <span>{openHealth.summary.revisionsRecent} revised</span>
              </div>
            </>
          )}

          <div className="source-health-controls">
            <button
              type="button"
              className="mini-btn"
              data-testid={`source-health-retry-${healthOpenId}`}
              disabled={busy === healthOpenId}
              onClick={() => { void actions.fetchNow(healthOpenId); }}
            >
              <RefreshCw size={12} className={busy === healthOpenId ? 'spin' : undefined} />
              Poll now
            </button>
            <span data-testid={`source-health-cost-${healthOpenId}`}>
              Cost {formatCost(openHealth.costs.recentActualMicro)}
            </span>
          </div>

          {activeTab === 'runs' && (
            <>
              <div className="source-health-section">
                <h4><ListChecks size={12} aria-hidden /> Recent runs</h4>
                <ul className="source-health-runs" data-testid={`source-health-runs-${healthOpenId}`}>
                  {openHealth.runs.length === 0 ? (
                    <li className="panel-empty">No runs yet.</li>
                  ) : openHealth.runs.map((run) => (
                    <li key={run.id} className="source-health-run">
                      <div>
                        <span className={`source-status ${statusClass(run.status)}`}>{statusLabel(run.status)}</span>
                        <span>{runDeltaText(run)}</span>
                      </div>
                      <div className="source-health-run-meta">
                        <span>{formatWhen(run.startedAt)}</span>
                        {formatDuration(run.durationMs) && <span>{formatDuration(run.durationMs)}</span>}
                        {run.warningCount > 0 && <span>{run.warningCount} warnings</span>}
                      </div>
                      {run.errorSummary && <div className="source-run-error">{run.errorSummary}</div>}
                    </li>
                  ))}
                </ul>
              </div>

              <div className="source-health-section">
                <h4><Activity size={12} aria-hidden /> Downstream jobs</h4>
                {openHealth.downstreamJobs.length === 0 ? (
                  <PanelEmpty testId={`source-health-jobs-empty-${healthOpenId}`}>
                    No downstream jobs recorded for loaded runs.
                  </PanelEmpty>
                ) : (
                  <ul className="source-health-jobs" data-testid={`source-health-jobs-${healthOpenId}`}>
                    {openHealth.downstreamJobs.map((job) => (
                      <li key={job.jobId}>
                        <span>{job.kind}</span>
                        <span className={`source-status ${statusClass(job.status)}`}>{statusLabel(job.status)}</span>
                        {job.errorSummary && <span className="source-run-error">{job.errorSummary}</span>}
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              {(openHealth.alerts.length > 0 || openHealth.warnings.length > 0) && (
                <div className="source-health-section" data-testid={`source-health-warnings-${healthOpenId}`}>
                  <h4><AlertTriangle size={12} aria-hidden /> Warnings</h4>
                  {[...openHealth.alerts, ...openHealth.warnings].map((notice) => (
                    <div key={`${notice.code ?? 'notice'}:${notice.message}`} className="source-health-notice">
                      {notice.message}
                    </div>
                  ))}
                </div>
              )}

              <div className="source-health-redactions" data-testid={`source-health-redactions-${healthOpenId}`}>
                Redacted: {openHealth.source.redactions.join(', ') || 'none'} / cursor {openHealth.summary.lastCursorSummary}
              </div>
            </>
          )}
        </>
      ) : null}
    </aside>
  );
}

export function SourceHealthMainView({
  sourceApi,
  sourceId,
  onClose,
  onPolled,
}: {
  sourceApi: SourceApiPort;
  sourceId: number;
  onClose: () => void;
  onPolled?: (sheetId: number | null) => void;
}) {
  const [state, dispatch] = useReducer(
    sourceHealthMainReducer,
    undefined,
    initialSourceHealthMainState,
  );
  const { health, loading, error, busy } = state;

  const loadHealth = useCallback(async () => {
    dispatch({ type: 'loadStart' });
    try {
      dispatch({ type: 'loadSuccess', health: await sourceApi.getSourceHealth(sourceId, 0, 20) });
    } catch (e) {
      dispatch({ type: 'loadFailure', message: msg(e) });
    }
  }, [sourceApi, sourceId]);

  useEffect(() => {
    let alive = true;
    void sourceApi.getSourceHealth(sourceId, 0, 20)
      .then((next) => {
        if (!alive) return;
        dispatch({ type: 'loadSuccess', health: next });
      })
      .catch((e) => {
        if (!alive) return;
        dispatch({ type: 'loadFailure', message: msg(e) });
      });
    return () => { alive = false; };
  }, [sourceApi, sourceId]);

  const fetchNow = useCallback(async () => {
    dispatch({ type: 'fetchStart' });
    try {
      const result = await sourceApi.fetchSource(sourceId);
      onPolled?.(result.sheetId);
    } catch (e) {
      dispatch({ type: 'fetchFailure', message: `Poll failed: ${msg(e)}` });
    } finally {
      dispatch({ type: 'fetchSettled' });
      await loadHealth();
    }
  }, [loadHealth, onPolled, sourceApi, sourceId]);

  return (
    <section className="source-health-mainView" data-testid="source-health-mainView">
      <div className="source-health-head">
        <div>
          <div className="source-health-title">{health?.source.name ?? 'Source health'}</div>
          {health && <div className="source-health-subtitle">{sourceKindLabel(health.source.kind)}</div>}
        </div>
        <button
          type="button"
          className="icon-btn"
          aria-label="Close source health"
          onClick={onClose}
        >
          <X size={14} />
        </button>
      </div>

      {error && (
        <PanelError testId="source-health-mainView-error">{error}</PanelError>
      )}
      {loading && !health ? (
        <PanelEmpty>loading...</PanelEmpty>
      ) : health ? (
        <>
          <div className="source-health-summary" data-testid={`source-health-summary-${sourceId}`}>
            <div>
              <span className="source-health-label">Status</span>
              <span
                className={`source-status ${statusClass(health.summary.status)}`}
                data-testid={`source-health-status-${sourceId}`}
              >
                {statusLabel(health.summary.status)}
              </span>
            </div>
            <div>
              <span className="source-health-label">Last success</span>
              <strong data-testid={`source-health-last-success-${sourceId}`}>
                {formatWhen(health.summary.lastSuccessAt)}
              </strong>
            </div>
            <div>
              <span className="source-health-label">Last failure</span>
              <strong data-testid={`source-health-last-failure-${sourceId}`}>
                {formatWhen(health.summary.lastFailureAt)}
              </strong>
            </div>
            <div>
              <span className="source-health-label">Failures</span>
              <strong data-testid={`source-health-consecutive-failures-${sourceId}`}>
                {health.summary.consecutiveFailures}
              </strong>
            </div>
          </div>

          <div className="source-health-deltas" data-testid={`source-health-deltas-${sourceId}`}>
            <span>+{health.summary.newRowsRecent} new</span>
            <span>{health.summary.changedRowsRecent} changed</span>
            <span>{health.summary.skippedRowsRecent} skipped</span>
            <span>{health.summary.revisionsRecent} revised</span>
          </div>

          <div className="source-health-controls">
            <button
              type="button"
              className="mini-btn"
              data-testid={`source-health-retry-${sourceId}`}
              disabled={busy}
              onClick={() => { void fetchNow(); }}
            >
              <RefreshCw size={12} className={busy ? 'spin' : undefined} />
              Poll now
            </button>
            <span data-testid={`source-health-cost-${sourceId}`}>
              Cost {formatCost(health.costs.recentActualMicro)}
            </span>
          </div>

          <div className="source-health-section">
            <h4><ListChecks size={12} aria-hidden /> Recent runs</h4>
            <ul className="source-health-runs" data-testid={`source-health-runs-${sourceId}`}>
              {health.runs.length === 0 ? (
                <li className="panel-empty">No runs yet.</li>
              ) : health.runs.map((run) => (
                <li key={run.id} className="source-health-run">
                  <div>
                    <span className={`source-status ${statusClass(run.status)}`}>{statusLabel(run.status)}</span>
                    <span>{runDeltaText(run)}</span>
                  </div>
                  <div className="source-health-run-meta">
                    <span>{formatWhen(run.startedAt)}</span>
                    {formatDuration(run.durationMs) && <span>{formatDuration(run.durationMs)}</span>}
                    {run.warningCount > 0 && <span>{run.warningCount} warnings</span>}
                  </div>
                  {run.errorSummary && <div className="source-run-error">{run.errorSummary}</div>}
                </li>
              ))}
            </ul>
          </div>

          <div className="source-health-section">
            <h4><Activity size={12} aria-hidden /> Downstream jobs</h4>
            {health.downstreamJobs.length === 0 ? (
              <PanelEmpty testId={`source-health-jobs-empty-${sourceId}`}>
                No downstream jobs recorded for loaded runs.
              </PanelEmpty>
            ) : (
              <ul className="source-health-jobs" data-testid={`source-health-jobs-${sourceId}`}>
                {health.downstreamJobs.map((job) => (
                  <li key={job.jobId}>
                    <span>{job.kind}</span>
                    <span className={`source-status ${statusClass(job.status)}`}>{statusLabel(job.status)}</span>
                    {job.errorSummary && <span className="source-run-error">{job.errorSummary}</span>}
                  </li>
                ))}
              </ul>
            )}
          </div>

          {(health.alerts.length > 0 || health.warnings.length > 0) && (
            <div className="source-health-section" data-testid={`source-health-warnings-${sourceId}`}>
              <h4><AlertTriangle size={12} aria-hidden /> Warnings</h4>
              {[...health.alerts, ...health.warnings].map((notice) => (
                <div key={`${notice.code ?? 'notice'}:${notice.message}`} className="source-health-notice">
                  {notice.message}
                </div>
              ))}
            </div>
          )}

          <div className="source-health-redactions" data-testid={`source-health-redactions-${sourceId}`}>
            Redacted: {health.source.redactions.join(', ') || 'none'} / cursor {health.summary.lastCursorSummary}
          </div>
        </>
      ) : null}
    </section>
  );
}

export function SourcesPanel({
  sourceApi,
  onPolled,
  onOpenSourceHealthMainView,
  onOpenImportDialog,
  sourceKindFormFrame,
  sourceDetailFrames,
  sourceDetailContributions,
  renderPluginDetailTab,
}: SourcesPanelProps) {
  const { state, actions } = useSourcesPanelState(sourceApi, onPolled);
  const {
    items,
    runs,
    runPages,
    expanded,
    form,
    busy,
    armedDelete,
    healthOpenId,
    healthLoading,
    health,
    healthError,
    error,
  } = state;
  const healthSource = healthOpenId == null ? null : items?.find((item) => item.id === healthOpenId) ?? null;
  const openHealth = healthOpenId == null ? null : health[healthOpenId] ?? null;

  return (
    <section className="sidebar-sources" data-testid="sources">
      <div className="sources-body" data-testid="sources-panel">
        {onOpenImportDialog && (
          <div className="sources-panel-head">
            <button
              type="button"
              className="btn btn-primary mini-btn"
              data-testid="sources-add-source"
              onClick={onOpenImportDialog}
            >
              <Plus size={13} aria-hidden /> Add source
            </button>
          </div>
        )}

        {error && (
          <PanelError testId="sources-error">{error}</PanelError>
        )}

        {form.editingId != null && (
          <SourceEditorForm
            actions={actions}
            busy={busy}
            form={form}
            formFrame={sourceKindFormFrame}
          />
        )}

        <SourceList
          actions={actions}
          armedDelete={armedDelete}
          busy={busy}
          expanded={expanded}
          items={items}
          runPages={runPages}
          runs={runs}
        />

        <SourceHealthDrawer
          actions={actions}
          busy={busy}
          healthError={healthError}
          healthLoading={healthLoading}
          healthOpenId={healthOpenId}
          healthSource={healthSource}
          openHealth={openHealth}
          onOpenSourceHealthMainView={onOpenSourceHealthMainView}
          sourceDetailFrames={sourceDetailFrames}
          sourceDetailContributions={sourceDetailContributions}
          renderPluginDetailTab={renderPluginDetailTab}
        />
      </div>
    </section>
  );
}

export interface SourcesConnectionsDialogProps extends SourcesPanelProps {
  open: boolean;
  onClose(): void;
}

/**
 * Focused project-level home for recurring sources and external connections.
 * The management body remains SourcesPanel, so modal and secondary Discover
 * placements share one list/status/edit/poll/delete implementation.
 */
export function SourcesConnectionsDialog({
  open,
  onClose,
  ...panelProps
}: SourcesConnectionsDialogProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
    const focusFrame = window.requestAnimationFrame(() => {
      closeButtonRef.current?.focus();
    });
    return () => window.cancelAnimationFrame(focusFrame);
  }, [open]);

  if (!open) return null;

  return (
    <dialog
      ref={dialogRef}
      className="sources-connections-layer"
      data-testid="sources-connections-dialog"
      aria-labelledby="sources-connections-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <button
        type="button"
        className="sources-connections-backdrop"
        aria-label="Close Sources & connections"
        onClick={onClose}
      />
      <section className="sources-connections" data-testid="sources-connections">
        <header className="sources-connections-header">
          <div className="sources-connections-heading">
            <span className="sources-connections-icon"><Database size={18} aria-hidden /></span>
            <div>
              <h2 id="sources-connections-title">Sources &amp; connections</h2>
              <p>Manage recurring feeds, polling, and connection health for this project.</p>
            </div>
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            className="icon-btn"
            aria-label="Close Sources & connections"
            data-testid="sources-connections-close"
            onClick={onClose}
          >
            <X size={16} />
          </button>
        </header>
        <main className="sources-connections-content">
          <SourcesPanel {...panelProps} />
        </main>
      </section>
    </dialog>
  );
}
