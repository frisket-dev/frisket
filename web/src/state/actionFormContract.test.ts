import { describe, expect, it } from 'vitest';
import type { ActionCatalogPayload } from '../api/open';
import { actionTemplatesFromCatalog } from '../actions/model';
import { buildCanonicalActionDraft, canonicalActionDraftFromValidatedParams,
  serializeCanonicalActionDraft } from '../actions/canonicalActionDraft';
import { actionTemplateFor, completeCatalogPayload } from '../../tests/support/actionFormFixtures';

describe('action form contract boundary', () => {
  const hydratedExtraOpts = {
    writesubtitles: true,
    writeautomaticsub: true,
    subtitleslangs: ['en', 'es'],
    writethumbnail: true,
    writeinfojson: true,
    merge_output_format: 'mp4',
    retries: 7,
    sleep_interval: 2,
  };

  it('hydrates and serializes Download media options as structured Params', () => {
    const entry = completeCatalogPayload().actions.find(({ kind }) => kind === 'media.ytdlp_download')!;
    const params = { source: 'url', media_type: 'audio', extra_opts: hydratedExtraOpts };
    const draft = canonicalActionDraftFromValidatedParams(entry, params);
    expect(serializeCanonicalActionDraft(draft)).toEqual(params);
    expect(draft.extra_opts).not.toBe(hydratedExtraOpts);
    expect(draft).not.toHaveProperty('yt_adv_writesubtitles');
  });

  it('declares video as the catalog default without materialization fields in Params', () => {
    const entry = completeCatalogPayload().actions.find(({ kind }) => kind === 'media.ytdlp_download')!;
    expect(entry.input_schema.properties).toMatchObject({ media_type: { default: 'video' } });
    expect(Object.keys(entry.input_schema.properties as object)).toEqual([
      'source', 'media_type', 'format_selector', 'extra_opts',
    ]);
  });

  it('builds canonical Params without leaking local-only controls or request fields', () => {
    const draft = buildCanonicalActionDraft(actionTemplateFor('media.ytdlp_download'), {
      source: 'url', media_type: 'audio', extra_opts: hydratedExtraOpts,
      yt_adv_writesubtitles: 'true', confirmed: true, output_name: 'media',
    });
    expect(serializeCanonicalActionDraft(draft)).toEqual({
      source: 'url', media_type: 'audio', extra_opts: hydratedExtraOpts,
    });
  });

  it('keeps column-table export out of generic ActionForm templates', () => {
    const catalog = {
      schema_version: 'frisket.action_catalog.v2',
      action_schema: {},
      actions: [
        {
          kind: 'export.column_tables',
          title: 'Export column tables',
          description: 'Package list-valued cells as CSV files in a ZIP.',
          input_schema: {},
          output_schema: {},
          errors: [],
          side_effects: [],
          required_capabilities: ['project:read', 'project:write'],
          cost_policy: { kind: 'none' },
          idempotency: { supported: true },
          execution_mode: 'whole_sheet',
          async_mode: 'sync',
          writes_project: true,
          examples: [],
          ui_hints: { form: 'column_tables_export' },
        },
      ],
    } as unknown as ActionCatalogPayload;

    expect(actionTemplatesFromCatalog(catalog)).toEqual([]);
  });
});
