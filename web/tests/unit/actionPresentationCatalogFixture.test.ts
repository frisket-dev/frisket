import { describe, expect, it } from 'vitest';

import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { hasNoActionDrawer } from '../../src/actions/registry';
import type {
  ActionCatalogEntry,
  ActionCatalogPayload,
  ActionTemplate,
} from '../../src/api/types';
import {
  ActionPresentationCatalogError,
  deriveActionPresentationCatalog,
} from '../../src/components/action-panel/actionPresentation';
import {
  completeMappedActionCatalog,
  syntheticActionCatalogEntry,
} from '../support/actionCatalogFixtures';
import {
  hasServedActionCatalogPython,
  servedActionCatalog,
} from '../support/servedActionCatalog';

const itServedCatalog = hasServedActionCatalogPython() || process.env.CI ? it : it.skip;

describe('compact mapped action-catalog fixture', () => {
  it('builds the exact mapped catalog through production presentation boundaries', () => {
    const payload = completeMappedActionCatalog();
    const dispositions = deriveActionPresentationCatalog(
      payload,
      actionTemplatesFromCatalog(payload),
    );
    expect(dispositions.size).toBe(payload.actions.length);
    for (const entry of payload.actions) {
      const expected = entry.ui_hints.form === 'generated' && !hasNoActionDrawer(entry.kind)
        ? 'generated'
        : 'hidden';
      expect(dispositions.get(entry.kind)?.kind, entry.kind).toBe(expected);
    }
  });

  it('supports explicit replacements and stock extras but rejects ambiguous extras', () => {
    const replacement = syntheticActionCatalogEntry('map.summarize', {
      title: 'Focused summarize fixture',
    });
    const hidden = syntheticActionCatalogEntry('derive.hidden_example');
    const payload = completeMappedActionCatalog({
      replacements: [replacement],
      stockEntries: [hidden],
    });
    expect(payload.actions.find((entry) => entry.kind === replacement.kind)).toBe(
      replacement,
    );
    expect(payload.actions.at(-1)).toBe(hidden);

    expect(() => completeMappedActionCatalog({
      replacements: [replacement, structuredClone(replacement)],
    })).toThrow(/more than once/);
    expect(() => completeMappedActionCatalog({
      replacements: [{ ...replacement, kind: 'plugin.example.clean' }],
    })).toThrow(/plugin/i);
  });
});

const catalog = completeMappedActionCatalog({
  stockEntries: [syntheticActionCatalogEntry('map.clean_column'), syntheticActionCatalogEntry('web.capture_page')],
});

function templatesFor(payload: ActionCatalogPayload): ActionTemplate[] {
  return actionTemplatesFromCatalog(payload);
}

function derive(
  payload: ActionCatalogPayload = catalog,
  templates: readonly ActionTemplate[] = templatesFor(payload),
) {
  return deriveActionPresentationCatalog(payload, templates);
}

function clonedEntry(
  kind: string,
  payload: ActionCatalogPayload = catalog,
): ActionCatalogEntry {
  const entry = payload.actions.find((candidate) => candidate.kind === kind);
  if (!entry) throw new Error(`Missing fixture entry ${kind}`);
  return structuredClone(entry);
}

function legacyPluginCatalog(): {
  payload: ActionCatalogPayload;
  pluginEntry: ActionCatalogEntry;
} {
  const mappedAction = clonedEntry('map.summarize');
  const pluginEntry: ActionCatalogEntry = {
    ...mappedAction,
    kind: 'plugin.example.clean',
    title: 'Plugin clean',
    // Project plugin catalog entries intentionally publish a valid object
    // schema without a `properties` member; their fields are declared by
    // ui_hints.form_params.
    input_schema: { type: 'object' },
    ui_hints: {
      plugin_action: true,
      form_params: [],
      source_requirements: [],
    },
  };
  return {
    pluginEntry,
    payload: { ...catalog, actions: [...catalog.actions, pluginEntry] },
  };
}

describe('catalog-derived action-presentation completeness', () => {
  it('admits entity-table and sibling generated templates without dropping validation', () => {
    const entry = syntheticActionCatalogEntry('resolve.entities', {
      input_schema: {
        type: 'object', required: ['source'], properties: {
          source: { type: 'object', required: ['kind', 'receipt_id'], properties: {
            kind: { type: 'string', enum: ['cluster_values'] },
            receipt_id: { type: 'string' },
          } },
        },
      },
      ui_hints: {
        form: 'generated', semantic_controls: {},
        logical_outputs: [{ key: 'entity', column_type: 'text' }],
        typed_action: { creates_sheet: true },
      },
    });
    const sibling = { ...entry, kind: 'example.structured_table' };
    const payload = completeMappedActionCatalog({ stockEntries: [entry, sibling] });
    const templates = templatesFor(payload);
    const dispositions = derive(payload, templates);
    expect(dispositions.get(entry.kind)).toEqual({ kind: 'generated' });
    expect(dispositions.get(sibling.kind)).toEqual({ kind: 'generated' });
    expect(() => derive(payload, templates.filter((template) => template.actionKind !== entry.kind)))
      .toThrow('generated action resolve.entities has no canonical launcher template');
  });

  itServedCatalog('partitions every stock entry through the production catalog derivation', () => {
    const servedCatalog = servedActionCatalog();
    expect(servedActionCatalog()).toBe(servedCatalog);
    const dispositions = derive(servedCatalog);
    expect(servedCatalog.schema_version).toBe('frisket.action_catalog.v2');
    expect(dispositions.size).toBe(servedCatalog.actions.length);
    expect(new Set(servedCatalog.actions.map((entry) => entry.kind)).size)
      .toBe(servedCatalog.actions.length);
    expect(servedCatalog.actions.every((entry) => entry.authoring_contract_version === 1))
      .toBe(true);

    for (const entry of servedCatalog.actions) {
      expect(dispositions.get(entry.kind), entry.kind).toBeDefined();
    }

    expect(dispositions.get('web.capture_page')).toEqual({ kind: 'generated' });
    expect(dispositions.get('web.capture_screenshot')).toEqual({ kind: 'generated' });
    expect(dispositions.get('derive.join')).toEqual({ kind: 'generated' });
    expect(dispositions.get('join.semantic')).toEqual({ kind: 'generated' });
    expect(dispositions.get('derive.table_from_list')).toEqual({
      kind: 'generated',
    });
    // Typed artifact exports retain their modal/pickers, not a generic drawer
    // or the retired export_column_tables launcher alias.
    expect(servedCatalog.actions.find((entry) => entry.kind === 'export.column_tables')
      ?.ui_hints.form).toBe('column_tables_export');
    expect(dispositions.get('export.column_tables')).toEqual({
      kind: 'hidden',
      reason: 'no_action_drawer_launcher',
    });
    expect(dispositions.get('plugin.load')).toEqual({
      kind: 'hidden',
      reason: 'no_action_drawer_launcher',
    });
    // Index export is configured by the embedding card, not a row-output form.
    expect(servedCatalog.actions.find((entry) => entry.kind === 'embedding.index_export')
      ?.ui_hints.form).toBe('embedding_index_export');
    expect(dispositions.get('embedding.index_export')).toEqual({
      kind: 'hidden',
      reason: 'no_action_drawer_launcher',
    });
    expect(dispositions.get('resolve.fill_missing')).toEqual({ kind: 'generated' });
    expect(dispositions.get('resolve.entities')).toEqual({ kind: 'generated' });
    expect(dispositions.get('map.ask')).toEqual({ kind: 'generated' });
    expect(dispositions.get('map.find_visual_cuts')).toEqual({ kind: 'generated' });
    expect(dispositions.get('map.summarize')).toEqual({ kind: 'generated' });
    expect(templatesFor(servedCatalog).find((template) => template.actionKind === 'map.ask'))
      .toMatchObject({ kind: 'map.ask', generatedAction: true });
    expect(templatesFor(servedCatalog).find((template) => template.actionKind === 'map.summarize'))
      .toMatchObject({ kind: 'map.summarize', generatedAction: true });
    expect(dispositions.get('map.template')).toEqual({ kind: 'generated' });
    expect(dispositions.get('map.clean_dates')).toEqual({ kind: 'generated' });
    expect(dispositions.get('map.to_geo_point')).toEqual({ kind: 'generated' });
    expect(dispositions.get('map.columns_from_json')).toEqual({ kind: 'generated' });
    expect(dispositions.get('map.python')?.kind).toBe('generated');
    expect(dispositions.get('derive.collection_expand')?.kind).toBe('hidden');
    expect(dispositions.get('cluster.values')?.kind).toBe('generated');
    expect(dispositions.get('temporal.extract_range')?.kind).toBe('generated');
    expect(dispositions.get('derive.temporal_segments')?.kind).toBe('generated');
    expect(dispositions.get('derive.transcript_segments')?.kind).toBe('generated');
    expect(dispositions.get('media.extract_pdf_tables')?.kind).toBe('generated');

    expect(dispositions.get('enrich.geocode')).toEqual({ kind: 'generated' });
    expect(dispositions.get('enrich.census_demographics')).toEqual({ kind: 'generated' });
    for (const kind of ['enrich.geocode', 'enrich.census_demographics']) {
      expect(templatesFor(servedCatalog).filter((template) => template.actionKind === kind))
        .toMatchObject([{ kind, generatedAction: true }]);
    }
  }, 15_000);

  it('does not admit legacy plugin-action catalog entries', () => {
    const { payload } = legacyPluginCatalog();

    expect(() => derive(payload)).toThrow(ActionPresentationCatalogError);
  });

  it.each(['web.capture_page', 'map.clean_column'])(
    'rejects a pre-floor stock entry: %s',
    (kind) => {
      const payload: ActionCatalogPayload = {
        ...catalog,
        actions: catalog.actions.map((entry) => entry.kind === kind
          ? { ...entry, authoring_contract_version: 0 }
          : entry),
      };

      expect(() => derive(payload)).toThrow(ActionPresentationCatalogError);
    },
  );

  it('rejects a resolved-template version that disagrees with its catalog entry', () => {
    const templates = templatesFor(catalog).map((template) => (
      template.actionKind === 'map.clean_column'
        ? {
            ...template,
            authoringContractVersion: 0,
          } as ActionTemplate
        : template
    ));

    expect(() => derive(catalog, templates)).toThrow(ActionPresentationCatalogError);
  });

  it('fails closed when a stock entry omits schema properties', () => {
    const payload: ActionCatalogPayload = {
      ...catalog,
      actions: catalog.actions.map((entry) => entry.kind === 'map.clean_column'
        ? { ...entry, input_schema: { type: 'object' } }
        : entry),
    };

    expect(() => derive(payload)).toThrow(
      'action catalog entry map.clean_column has malformed input_schema',
    );
  });

  it.each([
    ['malformed empty kind', (payload: ActionCatalogPayload) => ({
      ...payload,
      actions: [{ ...clonedEntry('map.summarize'), kind: '' }, ...payload.actions.slice(1)],
    })],
    ['malformed ui_hints', (payload: ActionCatalogPayload) => ({
      ...payload,
      actions: [{
        ...clonedEntry('map.summarize'),
        ui_hints: null as unknown as ActionCatalogEntry['ui_hints'],
      }, ...payload.actions.slice(1)],
    })],
    ['malformed input_schema', (payload: ActionCatalogPayload) => ({
      ...payload,
      actions: [{
        ...clonedEntry('map.summarize'),
        input_schema: null as unknown as ActionCatalogEntry['input_schema'],
      }, ...payload.actions.slice(1)],
    })],
    ['duplicate served kind', (payload: ActionCatalogPayload) => ({
      ...payload,
      actions: [...payload.actions, clonedEntry('map.summarize')],
    })],
  ] as const)('fails closed for %s', (_label, mutate) => {
    const payload = mutate(catalog);
    expect(() => derive(payload)).toThrow(ActionPresentationCatalogError);
  });

  it('fails closed for a missing or duplicate resolved core template', () => {
    const templates = templatesFor(catalog);
    const withoutCleanColumn = templates.filter(
      (template) => template.actionKind !== 'map.clean_column',
    );
    expect(() => derive(catalog, withoutCleanColumn)).toThrow(
      ActionPresentationCatalogError,
    );

    const cleanColumn = templates.find(
      (template) => template.actionKind === 'map.clean_column',
    );
    if (!cleanColumn) throw new Error('Missing map.clean_column template');
    expect(() => derive(catalog, [...templates, { ...cleanColumn }])).toThrow(
      ActionPresentationCatalogError,
    );
  });

});
