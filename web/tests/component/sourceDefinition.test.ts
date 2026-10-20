import { describe, expect, it } from 'vitest';
import { buildSourceInput, emptyForm, emptySourceDefinition, sourceToForm, type KnownSourceKind } from '../../src/components/sources/sourceConfig';

describe('the source definition shared by import and editing', () => {
  it.each<[KnownSourceKind, string, Record<string, unknown>]>([
    ['rss', 'https://example.test/feed', { merge_strategy: 'append-new' }],
    ['youtube_playlist', 'https://youtube.com/playlist?list=123', { schema_version: 'frisket.source.youtube.v1', playlist_url: 'https://youtube.com/playlist?list=123', max_pages_per_poll: 2, max_items_per_poll: 3 }],
    ['youtube_channel', '@example', { schema_version: 'frisket.source.youtube.v1', channel_url: '@example', max_pages_per_poll: 2, max_items_per_poll: 3 }],
    ['api_list_dicts', 'https://example.test/api', { schema_version: 'frisket.source.api_list_dicts.v1', method: 'GET', url: 'https://example.test/api', list_path: '/items', item_id_path: '/id', updated_at_path: '/updated', schema_policy: 'additive', max_items: 4 }],
    ['courtlistener_docket', '123', { schema_version: 'frisket.source.courtlistener_docket.v1', docket_id: 123, keywords: ['injunction', 'settlement'], include_parties: false, include_documents: true, recap_pdf_policy: 'link_only', max_entries_per_poll: 5 }],
  ])('round trips %s config through create/edit without changing fields', (kind, url, config) => {
    const created = buildSourceInput({ ...emptySourceDefinition(''), name: ' Records ', kind, url,
      apiListPath: '/items', apiItemIdPath: '/id', apiUpdatedAtPath: '/updated', apiMaxItems: '4',
      youtubeMaxPages: '2', youtubeMaxItems: '3', courtKeywords: 'injunction, settlement',
      courtIncludeParties: false, courtMaxEntries: '5' });
    expect(created.config).toEqual(config);
    expect(created.schedule).toBeNull();
    expect(created.url).toBe(kind === 'courtlistener_docket' ? null : url);
    const edited = sourceToForm({ ...created, id: 7, name: 'Records', sheetId: null,
      url: created.url ?? null, config: created.config ?? {}, schedule: created.schedule ?? null, enabled: created.enabled ?? true,
      lastCheckedAt: null, lastStatus: null, newRowsTotal: 0 });
    expect(buildSourceInput(edited)).toEqual(created);
  });

  it.each(['0', '-1', '1.5', 'invalid'])('validates numeric docket max entries %s before serialization', (limit) => {
    expect(() => buildSourceInput({ ...emptySourceDefinition(''), kind: 'courtlistener_docket', name: 'Records', url: '123', courtMaxEntries: limit }))
      .toThrow('Max entries must be a positive whole number');
  });

  it.each(['apiListPath', 'apiItemIdPath', 'apiUpdatedAtPath'] as const)('validates %s JSON pointers in both flows', (field) => {
    const draft = { ...emptyForm(), kind: 'api_list_dicts' as const, name: 'Records', url: 'https://example.test/api' };
    expect(() => buildSourceInput({ ...draft, [field]: 'items' })).toThrow('must start with /');
    expect(() => buildSourceInput({ ...draft, [field]: '/bad~2escape' })).toThrow('invalid ~ escape');
    expect(() => buildSourceInput({ ...draft, [field]: '/escaped~0tilde/~1slash' })).not.toThrow();
  });

  it('keeps caller cadence/name policy and refuses to serialize a redacted RSS edit', () => {
    expect(emptyForm().cadence).toBe('@hourly');
    expect(emptySourceDefinition('').cadence).toBe('');
    expect(() => buildSourceInput({ ...emptyForm(), url: 'https://example.test/feed' })).toThrow('Source name is required');
    const form = sourceToForm({ id: 7, name: 'Records', kind: 'rss', url: 'https://user:secret@example.test/feed?token=private',
      config: { token: 'masked-secret' }, schedule: null, enabled: true, sheetId: null, lastCheckedAt: null, lastStatus: null, newRowsTotal: 0 });
    expect(form.url).toBe('');
    expect(JSON.stringify(form)).not.toContain('secret');
    expect(() => buildSourceInput(form)).toThrow('requires a source reference');
    expect(buildSourceInput({ ...form, url: 'https://example.test/feed' }).config).toEqual({ merge_strategy: 'append-new' });
  });
});
