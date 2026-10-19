import type { SourceConfig, SourceInfo, SourceInput, SourceInterval, SourceKind } from '../../api/open';

// Cadence presets - the only schedule strings the backend interval parser
// (jobs/sources.py::_schedule_interval) accepts, so the picker cannot produce an
// invalid cron. '' = manual / on-demand (a null schedule server-side).
export const INTERVALS: { value: SourceInterval; label: string }[] = [
  { value: '', label: 'Manual' },
  { value: '@hourly', label: 'Hourly' },
  { value: '0 */6 * * *', label: 'Every 6 hours' },
  { value: '@daily', label: 'Daily' },
];

export type KnownSourceKind =
  | 'rss'
  | 'youtube_playlist'
  | 'youtube_channel'
  | 'api_list_dicts'
  | 'courtlistener_docket';

export const SOURCE_KINDS: Array<{
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

export interface SourceFormState {
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

export const emptyForm = (): SourceFormState => ({
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

export function sourceToForm(source: SourceInfo): SourceFormState {
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

export function buildSourceInput(form: SourceFormState): SourceInput {
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

export function sourceKindLabel(kind: SourceKind): string {
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

export function sourceConfigSummary(source: SourceInfo): string {
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

export function selectedKind(form: SourceFormState) {
  return SOURCE_KINDS.find((kind) => kind.value === form.kind) ?? SOURCE_KINDS[0];
}
