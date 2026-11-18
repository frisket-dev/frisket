import type { FeedConfig, FeedInfo, FeedInput, FeedInterval, FeedKind } from '../../api/open';

// Cadence presets - the only schedule strings the backend interval parser
// (jobs/sources.py::_schedule_interval) accepts, so the picker cannot produce an
// invalid cron. '' = manual / on-demand (a null schedule server-side).
export const INTERVALS: { value: FeedInterval; label: string }[] = [
  { value: '', label: 'Manual' },
  { value: '@hourly', label: 'Hourly' },
  { value: '0 */6 * * *', label: 'Every 6 hours' },
  { value: '@daily', label: 'Daily' },
];

export type KnownFeedKind =
  | 'rss'
  | 'youtube_playlist'
  | 'youtube_channel'
  | 'tiktok_profile'
  | 'api_list_dicts'
  | 'courtlistener_docket';

export const FEED_KINDS: Array<{
  value: KnownFeedKind;
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
    value: 'tiktok_profile',
    label: 'TikTok profile',
    urlLabel: 'Profile URL or @handle',
    placeholder: 'https://www.tiktok.com/@example',
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

const SUPPORTED_KIND_VALUES = new Set<KnownFeedKind>(
  FEED_KINDS.map((kind) => kind.value),
);

export interface FeedDefinitionDraft {
  name: string;
  kind: KnownFeedKind;
  url: string;
  cadence: FeedInterval;
  apiListPath: string;
  apiItemIdPath: string;
  apiUpdatedAtPath: string;
  apiMaxItems: string;
  youtubeMaxPages: string;
  mediaMaxItems: string;
  courtKeywords: string;
  courtIncludeParties: boolean;
  courtIncludeDocuments: boolean;
  courtMaxEntries: string;
}

export interface FeedFormState extends FeedDefinitionDraft {
  adding: boolean;
  editingId: number | null;
}

export const emptyFeedDefinition = (cadence: FeedInterval): FeedDefinitionDraft => ({
  name: '',
  kind: 'rss',
  url: '',
  cadence,
  apiListPath: '/',
  apiItemIdPath: '',
  apiUpdatedAtPath: '',
  apiMaxItems: '',
  youtubeMaxPages: '',
  mediaMaxItems: '',
  courtKeywords: '',
  courtIncludeParties: true,
  courtIncludeDocuments: true,
  courtMaxEntries: '',
});

export const emptyForm = (): FeedFormState => ({
  ...emptyFeedDefinition('@hourly'), adding: false, editingId: null,
});

function normalizeKind(kind: FeedKind): KnownFeedKind {
  return SUPPORTED_KIND_VALUES.has(kind as KnownFeedKind)
    ? kind as KnownFeedKind
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

function feedConfigObject(config: FeedConfig) {
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

export function redactedUrl(value: string | null | undefined): string | null {
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

export function feedToForm(feed: FeedInfo): FeedFormState {
  const kind = normalizeKind(feed.kind);
  const config = feedConfigObject(feed.config);
  const keywords = Array.isArray(config.keywords)
    ? config.keywords.filter((item): item is string => typeof item === 'string').join(', ')
    : '';
  const feedRef = feed.url
    ?? configString(config, ['playlist_id', 'playlist_url', 'channel_handle', 'channel_id', 'channel_url', 'profile_url', 'docket_url', 'docket_id', 'url']);
  const visibleRef = kind === 'rss' && urlHasSensitiveParts(feedRef) ? '' : (feedRef ?? '');
  return {
    ...emptyForm(),
    adding: true,
    editingId: feed.id,
    name: feed.name,
    kind,
    url: kind === 'rss' ? editableUrl(visibleRef) : visibleRef,
    cadence: (feed.schedule ?? '') as FeedInterval,
    apiListPath: configString(config, ['list_path']) || '/',
    apiItemIdPath: configString(config, ['item_id_path']),
    apiUpdatedAtPath: configString(config, ['updated_at_path']),
    apiMaxItems: configNumber(config, 'max_items'),
    youtubeMaxPages: configNumber(config, 'max_pages_per_poll'),
    mediaMaxItems: configNumber(config, 'max_items_per_poll'),
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

function jsonPointerError(value: string, field: string): string | null {
  const text = value.trim() || '/';
  if (!text.startsWith('/')) return `${field} must start with /`;
  if (/~(?![01])/.test(text)) return `${field} has an invalid ~ escape`;
  return null;
}

export function feedDefinitionError(form: FeedDefinitionDraft): string | null {
  if (form.kind !== 'api_list_dicts') return null;
  if (form.url.trim() && !apiUrlIsHttps(form.url.trim())) return 'API list feeds require HTTPS';
  return jsonPointerError(form.apiListPath, 'Response list path')
    ?? (form.apiItemIdPath.trim() ? jsonPointerError(form.apiItemIdPath, 'Item ID path') : null)
    ?? (form.apiUpdatedAtPath.trim() ? jsonPointerError(form.apiUpdatedAtPath, 'Updated-at path') : null);
}

export function buildFeedInput(form: FeedDefinitionDraft): FeedInput {
  const name = form.name.trim();
  const url = form.url.trim();
  if (!name) throw new Error('Feed name is required');
  if (!url) throw new Error(`${feedKindLabel(form.kind)} requires a feed reference`);
  const validationError = feedDefinitionError(form);
  if (validationError) throw new Error(validationError);

  const base: FeedInput = {
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
      const maxItems = optionalPositiveInt(form.mediaMaxItems, 'Max items');
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
      const maxItems = optionalPositiveInt(form.mediaMaxItems, 'Max items');
      if (maxPages !== undefined) config.max_pages_per_poll = maxPages;
      if (maxItems !== undefined) config.max_items_per_poll = maxItems;
      return { ...base, config };
    }
    case 'tiktok_profile': {
      const config: Record<string, unknown> = { profile_url: url };
      const maxItems = optionalPositiveInt(form.mediaMaxItems, 'Max items');
      if (maxItems !== undefined) config.max_items_per_poll = maxItems;
      return { ...base, config };
    }
    case 'api_list_dicts': {
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

export function feedKindLabel(kind: FeedKind): string {
  return FEED_KINDS.find((candidate) => candidate.value === kind)?.label
    ?? String(kind).replaceAll('_', ' ');
}

export function feedConfigSummary(feed: FeedInfo): string {
  const config = feedConfigObject(feed.config);
  switch (feed.kind) {
    case 'youtube_playlist':
      return `playlist ${configString(config, ['playlist_id', 'playlist_url']) || redactedUrl(feed.url) || ''}`.trim();
    case 'youtube_channel':
      return `channel ${configString(config, ['channel_handle', 'channel_id', 'channel_url']) || redactedUrl(feed.url) || ''}`.trim();
    case 'tiktok_profile':
      return `profile ${configString(config, ['profile_url']) || redactedUrl(feed.url) || ''}`.trim();
    case 'api_list_dicts':
      return `list path ${configString(config, ['list_path']) || '/'}`;
    case 'courtlistener_docket': {
      const docket = configString(config, ['docket_id', 'docket_url']) || redactedUrl(feed.url) || '';
      const keywords = Array.isArray(config.keywords) && config.keywords.length > 0
        ? `, ${config.keywords.length} terms`
        : '';
      return `docket ${docket}${keywords}`;
    }
    default:
      return 'append-new';
  }
}

export function selectedKind(form: Pick<FeedDefinitionDraft, 'kind'>) {
  return FEED_KINDS.find((kind) => kind.value === form.kind) ?? FEED_KINDS[0];
}
