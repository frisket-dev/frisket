import { useReducer, useRef, useState } from 'react';
import { Globe2, ListVideo, RefreshCw, Rss, Scale } from 'lucide-react';
import type { SourceInfo, SourceInput, SourceInterval } from '../../api/open';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { durationBucket, failureCategory, sendProductTelemetry, type SourceKind } from '../../telemetry/productTelemetry';
import { Button } from '../Button';
import { FormField, TextInput, TextArea } from '../FormControls';
import { PanelSelect } from '../PanelSelect';
import { PanelFrame, PanelBody, PanelFooter } from '../PanelLayout';

type FeedSourceKind =
  | 'rss'
  | 'youtube_playlist'
  | 'youtube_channel'
  | 'api_list_dicts'
  | 'courtlistener_docket';

function telemetrySourceKind(kind: FeedSourceKind): SourceKind {
  if (kind === 'api_list_dicts') return 'api';
  if (kind === 'courtlistener_docket') return 'courtlistener';
  return kind;
}

const FEED_SOURCE_KINDS: Array<{
  value: FeedSourceKind;
  label: string;
  placeholder: string;
}> = [
  {
    value: 'rss',
    label: 'RSS',
    placeholder: 'https://example.com/feed.xml',
  },
  {
    value: 'youtube_playlist',
    label: 'YouTube playlist',
    placeholder: 'https://www.youtube.com/playlist?list=PL...',
  },
  {
    value: 'youtube_channel',
    label: 'YouTube channel',
    placeholder: 'https://www.youtube.com/@example',
  },
  {
    value: 'api_list_dicts',
    label: 'API list',
    placeholder: 'https://example.gov/api/items.json',
  },
  {
    value: 'courtlistener_docket',
    label: 'CourtListener docket',
    placeholder: 'https://www.courtlistener.com/docket/123456/example/',
  },
];

const INTERVALS: { value: SourceInterval; label: string }[] = [
  { value: '', label: 'Manual' },
  { value: '@hourly', label: 'Hourly' },
  { value: '0 */6 * * *', label: 'Every 6 hours' },
  { value: '@daily', label: 'Daily' },
];

interface FeedSourceFormState {
  name: string;
  url: string;
  kind: FeedSourceKind;
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
  busy: boolean;
  error: string | null;
}

type FeedSourceFormAction =
  | { type: 'setName'; value: string }
  | { type: 'setUrl'; value: string; detectedKind?: FeedSourceKind }
  | { type: 'setKind'; value: FeedSourceKind }
  | { type: 'setCadence'; value: SourceInterval }
  | { type: 'setApiListPath'; value: string }
  | { type: 'setApiItemIdPath'; value: string }
  | { type: 'setApiUpdatedAtPath'; value: string }
  | { type: 'setApiMaxItems'; value: string }
  | { type: 'setYoutubeMaxPages'; value: string }
  | { type: 'setYoutubeMaxItems'; value: string }
  | { type: 'setCourtKeywords'; value: string }
  | { type: 'setCourtIncludeParties'; value: boolean }
  | { type: 'setCourtIncludeDocuments'; value: boolean }
  | { type: 'setCourtMaxEntries'; value: string }
  | { type: 'createStart' }
  | { type: 'createFailure'; message: string }
  | { type: 'createSettled' };

const initialFeedSourceFormState = (): FeedSourceFormState => ({
  name: '',
  url: '',
  kind: 'rss',
  cadence: '',
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
  busy: false,
  error: null,
});

function feedSourceFormReducer(
  state: FeedSourceFormState,
  action: FeedSourceFormAction,
): FeedSourceFormState {
  switch (action.type) {
    case 'setName':
      return { ...state, name: action.value };
    case 'setUrl':
      return {
        ...state,
        url: action.value,
        kind: action.detectedKind ?? state.kind,
        error: null,
      };
    case 'setKind':
      return { ...state, kind: action.value, error: null };
    case 'setCadence':
      return { ...state, cadence: action.value };
    case 'setApiListPath':
      return { ...state, apiListPath: action.value, error: null };
    case 'setApiItemIdPath':
      return { ...state, apiItemIdPath: action.value };
    case 'setApiUpdatedAtPath':
      return { ...state, apiUpdatedAtPath: action.value };
    case 'setApiMaxItems':
      return { ...state, apiMaxItems: action.value };
    case 'setYoutubeMaxPages':
      return { ...state, youtubeMaxPages: action.value };
    case 'setYoutubeMaxItems':
      return { ...state, youtubeMaxItems: action.value };
    case 'setCourtKeywords':
      return { ...state, courtKeywords: action.value };
    case 'setCourtIncludeParties':
      return { ...state, courtIncludeParties: action.value };
    case 'setCourtIncludeDocuments':
      return { ...state, courtIncludeDocuments: action.value };
    case 'setCourtMaxEntries':
      return { ...state, courtMaxEntries: action.value };
    case 'createStart':
      return { ...state, busy: true, error: null };
    case 'createFailure':
      return { ...state, busy: false, error: action.message };
    case 'createSettled':
      return { ...state, busy: false };
    default:
      return state;
  }
}

function feedKindLabel(kind: FeedSourceKind): string {
  return FEED_SOURCE_KINDS.find((candidate) => candidate.value === kind)?.label ?? kind;
}

function feedKindIcon(kind: FeedSourceKind) {
  switch (kind) {
    case 'youtube_playlist':
    case 'youtube_channel':
      return <ListVideo size={14} aria-hidden />;
    case 'api_list_dicts':
      return <Globe2 size={14} aria-hidden />;
    case 'courtlistener_docket':
      return <Scale size={14} aria-hidden />;
    default:
      return <Rss size={14} aria-hidden />;
  }
}

function detectFeedSourceKind(raw: string): FeedSourceKind {
  const value = raw.trim();
  if (!value) return 'rss';
  if (/^[1-9][0-9]*$/.test(value)) return 'courtlistener_docket';
  if (/^@[\w.-]+$/.test(value)) return 'youtube_channel';
  try {
    const parsed = new URL(value);
    const host = parsed.hostname.toLowerCase().replace(/^www\./, '');
    const path = parsed.pathname.toLowerCase();
    if (host === 'youtube.com' || host === 'youtu.be' || host.endsWith('.youtube.com')) {
      if (parsed.searchParams.has('list') || path.includes('/playlist')) return 'youtube_playlist';
      if (
        host === 'youtu.be' ||
        path.startsWith('/@') ||
        path.startsWith('/channel/') ||
        path.startsWith('/c/') ||
        path.startsWith('/user/')
      ) {
        return 'youtube_channel';
      }
      return 'youtube_channel';
    }
    if (host === 'courtlistener.com' || host === 'www.courtlistener.com') {
      if (path.startsWith('/docket/')) return 'courtlistener_docket';
    }
    if (path.endsWith('.json') || path.includes('/api/') || path.endsWith('/api')) {
      return 'api_list_dicts';
    }
    if (
      path.endsWith('.xml') ||
      path.endsWith('.rss') ||
      path.endsWith('.atom') ||
      path.includes('/feed') ||
      path.includes('/rss') ||
      path.includes('/atom')
    ) {
      return 'rss';
    }
  } catch {
    // Free-form references such as YouTube handles and CourtListener ids are handled above.
  }
  return 'rss';
}

function optionalPositiveInt(value: string, field: string): number | undefined {
  if (!value.trim()) return undefined;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`${field} must be a positive whole number`);
  }
  return parsed;
}

function splitKeywords(value: string): string[] {
  return value.split(/[\n,]/).flatMap((item) => {
    const trimmed = item.trim();
    return trimmed ? [trimmed] : [];
  });
}

function apiUrlIsHttps(value: string): boolean {
  try {
    return new URL(value).protocol === 'https:';
  } catch {
    return false;
  }
}

function jsonPointerError(value: string, field: string): string | null {
  const text = value.trim() || '/';
  if (!text.startsWith('/')) return `${field} must start with /`;
  for (const segment of text.split('/').slice(1)) {
    for (let index = 0; index < segment.length; index += 1) {
      if (segment[index] !== '~') continue;
      const escaped = segment[index + 1];
      if (escaped !== '0' && escaped !== '1') return `${field} has an invalid ~ escape`;
      index += 1;
    }
  }
  return null;
}

function defaultFeedName(kind: FeedSourceKind, reference: string): string {
  const label = feedKindLabel(kind);
  const compact = reference
    .trim()
    .replace(/^https?:\/\//i, '')
    .replace(/^www\./i, '')
    .slice(0, 72);
  return compact ? `${label}: ${compact}` : label;
}

function buildFeedSourceInput({
  name,
  kind,
  url,
  cadence,
  apiListPath,
  apiItemIdPath,
  apiUpdatedAtPath,
  apiMaxItems,
  youtubeMaxPages,
  youtubeMaxItems,
  courtKeywords,
  courtIncludeParties,
  courtIncludeDocuments,
  courtMaxEntries,
}: {
  name: string;
  kind: FeedSourceKind;
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
}): SourceInput {
  const reference = url.trim();
  if (!reference) throw new Error('Feed URL or reference is required');
  const base: SourceInput = {
    name: name.trim() || defaultFeedName(kind, reference),
    kind,
    url: reference,
    schedule: cadence || null,
    enabled: true,
    config: {},
  };

  switch (kind) {
    case 'rss':
      // RSS uses the existing SourcesPanel-compatible unversioned config shape.
      return {
        ...base,
        config: { merge_strategy: 'append-new' },
      };
    case 'youtube_playlist': {
      const config: Record<string, unknown> = {
        schema_version: 'frisket.source.youtube.v1',
        playlist_url: reference,
      };
      const maxPages = optionalPositiveInt(youtubeMaxPages, 'Max pages');
      const maxItems = optionalPositiveInt(youtubeMaxItems, 'Max items');
      if (maxPages !== undefined) config.max_pages_per_poll = maxPages;
      if (maxItems !== undefined) config.max_items_per_poll = maxItems;
      return { ...base, config };
    }
    case 'youtube_channel': {
      const config: Record<string, unknown> = {
        schema_version: 'frisket.source.youtube.v1',
        channel_url: reference,
      };
      const maxPages = optionalPositiveInt(youtubeMaxPages, 'Max pages');
      const maxItems = optionalPositiveInt(youtubeMaxItems, 'Max items');
      if (maxPages !== undefined) config.max_pages_per_poll = maxPages;
      if (maxItems !== undefined) config.max_items_per_poll = maxItems;
      return { ...base, config };
    }
    case 'api_list_dicts': {
      if (!apiUrlIsHttps(reference)) throw new Error('API list sources require HTTPS');
      const listPathError = jsonPointerError(apiListPath, 'Response list path');
      if (listPathError) throw new Error(listPathError);
      const itemIdError = apiItemIdPath.trim()
        ? jsonPointerError(apiItemIdPath, 'Item ID path')
        : null;
      if (itemIdError) throw new Error(itemIdError);
      const updatedAtError = apiUpdatedAtPath.trim()
        ? jsonPointerError(apiUpdatedAtPath, 'Updated-at path')
        : null;
      if (updatedAtError) throw new Error(updatedAtError);
      const config: Record<string, unknown> = {
        schema_version: 'frisket.source.api_list_dicts.v1',
        method: 'GET',
        url: reference,
        list_path: apiListPath.trim() || '/',
        schema_policy: 'additive',
      };
      if (apiItemIdPath.trim()) config.item_id_path = apiItemIdPath.trim();
      if (apiUpdatedAtPath.trim()) config.updated_at_path = apiUpdatedAtPath.trim();
      const maxItems = optionalPositiveInt(apiMaxItems, 'Max items');
      if (maxItems !== undefined) config.max_items = maxItems;
      return { ...base, config };
    }
    case 'courtlistener_docket': {
      const config: Record<string, unknown> = {
        schema_version: 'frisket.source.courtlistener_docket.v1',
        keywords: splitKeywords(courtKeywords),
        include_parties: courtIncludeParties,
        include_documents: courtIncludeDocuments,
        recap_pdf_policy: 'link_only',
      };
      if (/^[1-9][0-9]*$/.test(reference)) {
        config.docket_id = Number(reference);
        return { ...base, url: null, config };
      } else {
        config.docket_url = reference;
      }
      const maxEntries = optionalPositiveInt(courtMaxEntries, 'Max entries');
      if (maxEntries !== undefined) config.max_entries_per_poll = maxEntries;
      return { ...base, config };
    }
  }
}

interface FeedPopulateState {
  source: SourceInfo;
  busy: boolean;
  error: string | null;
}

export function FeedSourceForm({
  onCreated,
  onImported,
  onError,
  onCancel,
}: {
  onCreated?(): void;
  onImported(sheetId: number): void;
  onError(message: string): void;
  onCancel(): void;
}) {
  const { projectApi: api, chromePreferences: { projectId } } = useWorkspaceStores();
  const [state, dispatch] = useReducer(
    feedSourceFormReducer,
    undefined,
    initialFeedSourceFormState,
  );
  const kindTouchedRef = useRef(false);
  // A successful
  // create does NOT close the workspace — it swaps to a "Populate feed?"
  // prompt offering the same run this source's Sources-panel row would (Poll
  // now -> api.fetchSource). No watchRunLinkStore intent slot needed here: unlike
  // the notification -> WatchesPanel handoff (pendingWatchRunLink), this
  // prompt and its onImported target live in the same always-mounted
  // component tree for the lifetime of the dialog — a direct prop (already
  // threaded through ImportWorkspaceDialog) reaches the right place
  // synchronously, so there is no unmounted-target race to buffer against.
  const [populate, setPopulate] = useState<FeedPopulateState | null>(null);
  const {
    name,
    url,
    kind,
    cadence,
    apiListPath,
    apiItemIdPath,
    apiUpdatedAtPath,
    apiMaxItems,
    youtubeMaxPages,
    youtubeMaxItems,
    courtKeywords,
    courtIncludeParties,
    courtIncludeDocuments,
    courtMaxEntries,
    busy,
    error,
  } = state;
  const selectedKind = FEED_SOURCE_KINDS.find((candidate) => candidate.value === kind)
    ?? FEED_SOURCE_KINDS[0];
  const apiListPathError = kind === 'api_list_dicts'
    ? jsonPointerError(apiListPath, 'Response list path')
    : null;
  const apiUrlError = kind === 'api_list_dicts' && url.trim() && !apiUrlIsHttps(url)
    ? 'API list sources require HTTPS'
    : null;
  const validationError = apiUrlError ?? apiListPathError;

  const create = async () => {
    if (!url.trim() || busy || validationError) return;
    dispatch({ type: 'createStart' });
    const startedAt = performance.now();
    try {
      const created = await api.createSource(buildFeedSourceInput({
        name,
        kind,
        url,
        cadence,
        apiListPath,
        apiItemIdPath,
        apiUpdatedAtPath,
        apiMaxItems,
        youtubeMaxPages,
        youtubeMaxItems,
        courtKeywords,
        courtIncludeParties,
        courtIncludeDocuments,
        courtMaxEntries,
      }));
      window.dispatchEvent(new CustomEvent('frisket:sources-changed'));
      // Don't close yet -- surface the populate prompt instead (see the
      // FeedPopulateState comment above).
      setPopulate({ source: created, busy: false, error: null });
      sendProductTelemetry({
        type: 'Source.created',
        properties: {
          sourceKind: telemetrySourceKind(kind), requestDuration: durationBucket(performance.now() - startedAt),
          result: 'success', failureCategory: 'none',
        },
      }, projectId);
    } catch (e) {
      sendProductTelemetry({
        type: 'Source.created',
        properties: {
          sourceKind: telemetrySourceKind(kind), requestDuration: durationBucket(performance.now() - startedAt),
          result: 'failed', failureCategory: failureCategory(e),
        },
      }, projectId);
      const message = `Feed source failed: ${e instanceof Error ? e.message : String(e)}`;
      dispatch({ type: 'createFailure', message });
      onError(message);
    } finally {
      dispatch({ type: 'createSettled' });
    }
  };

  const runPopulate = async () => {
    if (!populate || populate.busy) return;
    setPopulate({ ...populate, busy: true, error: null });
    try {
      // The exact fetch a Sources-panel "Poll now" click makes
      // (SourcesPanel.tsx's fetchNow -> sourceApi.fetchSource).
      const result = await api.fetchSource(populate.source.id);
      if (result.sheetId != null) onImported(result.sheetId);
      onCreated?.();
    } catch (e) {
      const message = `Populate failed: ${e instanceof Error ? e.message : String(e)}`;
      setPopulate({ ...populate, busy: false, error: message });
    }
  };

  const dismissPopulate = () => {
    onCreated?.();
  };

  if (populate) {
    return (
      <PanelFrame fill>
      <PanelBody className="content-stack" data-testid="feed-populate-prompt">
        <div className="import-mode-summary">
          {feedKindIcon(kind)}
          <strong>Populate feed?</strong>
        </div>
        <p className="modal-body-text">
          Run <strong>{populate.source.name}</strong> now to pull in its rows
          — the same fetch as Poll now in Sources. You can run it again
          later either way.
        </p>
        {populate.error && (
          <div className="panel-error" role="alert">{populate.error}</div>
        )}
      </PanelBody>
      <PanelFooter>
          <Button
            type="button"

            data-testid="feed-populate-dismiss"
            disabled={populate.busy}
            onClick={dismissPopulate}
          >
            Not now
          </Button>
          <span>The feed is saved. Fetch its rows now or later.</span>
          <Button
            type="button"
            className="btn-primary import-primary"
            data-testid="feed-populate-run"
            disabled={populate.busy}
            onClick={() => void runPopulate()}
          >
            <RefreshCw size={13} className={populate.busy ? 'spin' : undefined} />
            {populate.busy ? 'Running...' : 'Run'}
          </Button>
      </PanelFooter>
      </PanelFrame>
    );
  }

  return (
    <PanelFrame fill>
    <PanelBody>
    <div className="content-stack" data-testid="source-form">
      <div className="import-mode-summary">
        {feedKindIcon(kind)}
        <strong>Feed</strong>
        <span>{feedKindLabel(kind)}</span>
      </div>
      <FormField label="URL or reference" htmlFor="source-url-input">
        <TextInput
          id="source-url-input"

          placeholder={selectedKind.placeholder}
          aria-label="Feed URL or reference"
          data-testid="source-url"
          value={url}
          onChange={(e) => {
            const next = e.target.value;
            dispatch({
              type: 'setUrl',
              value: next,
              detectedKind: kindTouchedRef.current ? undefined : detectFeedSourceKind(next),
            });
          }}
        />
      </FormField>
      <FormField label="Type" htmlFor="source-kind-input">
        <PanelSelect
          id="source-kind-input"
          className="form-input"
          aria-label="Source type"
          data-testid="source-kind"
          value={kind}
          onChange={(e) => {
            kindTouchedRef.current = true;
            dispatch({ type: 'setKind', value: e.target.value as FeedSourceKind });
          }}
        >
          {FEED_SOURCE_KINDS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </PanelSelect>
      </FormField>
      <FormField label="Name" htmlFor="source-name-input">
        <TextInput
          id="source-name-input"

          placeholder={defaultFeedName(kind, url)}
          aria-label="Source name"
          data-testid="source-name"
          value={name}
          onChange={(e) => dispatch({ type: 'setName', value: e.target.value })}
        />
      </FormField>

      {(kind === 'youtube_playlist' || kind === 'youtube_channel') && (
        <div className="sources-form-grid">
          <FormField label="Max pages" >

            <TextInput
              aria-label="YouTube max pages"
              data-testid="source-youtube-max-pages"
              value={youtubeMaxPages}
              onChange={(e) => dispatch({ type: 'setYoutubeMaxPages', value: e.target.value })}
            />
          </FormField>
          <FormField label="Max items" >

            <TextInput
              aria-label="YouTube max items"
              data-testid="source-youtube-max-items"
              value={youtubeMaxItems}
              onChange={(e) => dispatch({ type: 'setYoutubeMaxItems', value: e.target.value })}
            />
          </FormField>
        </div>
      )}

      {kind === 'api_list_dicts' && (
        <>
          <div className="sources-form-grid">
            <FormField label="Response list path" >

              <TextInput
                aria-label="API response list path"
                data-testid="source-api-list-path"
                value={apiListPath}
                onChange={(e) => dispatch({ type: 'setApiListPath', value: e.target.value })}
              />
            </FormField>
            <FormField label="Item ID path" >

              <TextInput
                aria-label="API item ID path"
                data-testid="source-api-item-id-path"
                value={apiItemIdPath}
                onChange={(e) => dispatch({ type: 'setApiItemIdPath', value: e.target.value })}
              />
            </FormField>
          </div>
          <div className="sources-form-grid">
            <FormField label="Updated path" >

              <TextInput
                aria-label="API updated-at path"
                data-testid="source-api-updated-at-path"
                value={apiUpdatedAtPath}
                onChange={(e) => dispatch({ type: 'setApiUpdatedAtPath', value: e.target.value })}
              />
            </FormField>
            <FormField label="Max items" >

              <TextInput
                aria-label="API max items"
                data-testid="source-api-max-items"
                value={apiMaxItems}
                onChange={(e) => dispatch({ type: 'setApiMaxItems', value: e.target.value })}
              />
            </FormField>
          </div>
        </>
      )}

      {kind === 'courtlistener_docket' && (
        <>
          <TextArea
            className="source-textarea"
            aria-label="CourtListener keywords"
            data-testid="source-court-keywords"
            value={courtKeywords}
            onChange={(e) => dispatch({ type: 'setCourtKeywords', value: e.target.value })}
            placeholder="injunction, settlement"
          />
          <div className="control-row">
            <label className="control-row">
              <input
                type="checkbox"
                data-testid="source-court-include-parties"
                checked={courtIncludeParties}
                onChange={(e) => dispatch({ type: 'setCourtIncludeParties', value: e.target.checked })}
              />
              Parties
            </label>
            <label className="control-row">
              <input
                type="checkbox"
                data-testid="source-court-include-documents"
                checked={courtIncludeDocuments}
                onChange={(e) => dispatch({ type: 'setCourtIncludeDocuments', value: e.target.checked })}
              />
              Documents
            </label>
          </div>
          <FormField label="Max entries">
            <TextInput
              aria-label="CourtListener max entries"
              data-testid="source-court-max-entries"
              value={courtMaxEntries}
              onChange={(e) => dispatch({ type: 'setCourtMaxEntries', value: e.target.value })}
            />
          </FormField>
        </>
      )}

      <FormField label="Update frequency" htmlFor="source-interval-input">
        <PanelSelect
          id="source-interval-input"
          className="form-input"
          aria-label="Update frequency"
          data-testid="source-interval"
          value={cadence}
          onChange={(e) => dispatch({ type: 'setCadence', value: e.target.value as SourceInterval })}
        >
          {INTERVALS.map((i) => (
            <option key={i.value} value={i.value}>{i.label}</option>
          ))}
        </PanelSelect>
      </FormField>
      {(validationError || error) && (
        <div className="panel-error" role="alert">
          {validationError ?? error}
        </div>
      )}
    </div>
    </PanelBody>
    <PanelFooter>
      <Button type="button" disabled={busy} onClick={onCancel}>Cancel</Button>
      <span>Add a feed to keep its source available for updates.</span>
      <Button
        type="button"
        className="btn-primary import-primary"
        data-testid="source-create"
        disabled={busy || !url.trim() || Boolean(validationError)}
        onClick={() => void create()}
      >
        {busy ? 'Adding...' : 'Add feed'}
      </Button>
    </PanelFooter>
    </PanelFrame>
  );
}
