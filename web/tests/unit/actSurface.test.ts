import { afterEach, describe, expect, it, vi } from 'vitest';
import { Braces, LocateFixed } from 'lucide-react';
import type { ActionTemplate } from '../../src/api/types';
import { actionTemplatesFromCatalog } from '../../src/actions/model';
import {
  ACTION_ICON_BY_KIND,
  BASE_ACT_TAB_IDS,
  resolveRibbonTabs,
} from '../../src/workbench/actSurface';
import {
  completeMappedActionCatalog,
  syntheticActionCatalogEntry,
} from '../support/actionCatalogFixtures';

const EMPTY_CONTEXT = {
  activeProject: false,
  activeSheet: false,
  activeRow: false,
  activeColumn: false,
  activeCell: false,
  activeEvidence: false,
  activeSource: false,
  activeEntity: false,
  activeProjection: false,
  selectedRowIds: [],
  columnTypes: [],
};

const METADATA_ENTRY = syntheticActionCatalogEntry('media.extract_metadata', {
  title: 'Extract media metadata',
  ui_hints: {
    form: 'generated', category: 'extract', semantic_controls: {},
    logical_outputs: [], dynamic_outputs: true,
  },
});
const CLUSTER_ENTRY = syntheticActionCatalogEntry('cluster.values');
const CAPTURE_ENTRY = syntheticActionCatalogEntry('web.capture_page');

function template(placement: ActionTemplate['placement']): ActionTemplate {
  return {
    kind: 'map.summarize',
    name: 'Summarize',
    description: 'Summarize',
    defaultPrompt: '',
    defaultFields: [],
    placement,
  };
}

function unlistedNativeTemplate(
  kind: string,
  placement?: ActionTemplate['placement'],
): ActionTemplate {
  return {
    kind: kind as ActionTemplate['kind'],
    name: 'Plugin action',
    description: 'Plugin action',
    defaultPrompt: '',
    defaultFields: [],
    placement,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('action icon semantics', () => {
  it('gives the formerly generic cleanup and utility actions distinct glyphs', () => {
    const kinds = [
      'map.api_call',
      'map.columns_from_json',
      'cluster.values',
      'resolve.substitute',
      'resolve.replace',
      'resolve.combine',
      'resolve.fill_missing',
      'export.column_tables',
    ];
    expect(new Set(kinds.map((kind) => ACTION_ICON_BY_KIND[kind])).size).toBe(kinds.length);
  });
});

describe('resolveRibbonTabs placement invariants', () => {
  it('keeps the permanent surface to six broad tabs', () => {
    expect(BASE_ACT_TAB_IDS).toEqual([
      'data', 'media', 'analyze', 'transform', 'tables', 'research',
    ]);
  });

  it.each([
    ['derive.join', 'derive_join'], ['join.semantic', 'semantic_join'],
  ])('places %s once in Tables under its canonical ID', (canonical, retired) => {
    const catalog = completeMappedActionCatalog({
      stockEntries: [syntheticActionCatalogEntry(canonical)],
    });
    const tabs = resolveRibbonTabs(actionTemplatesFromCatalog(catalog), EMPTY_CONTEXT);
    const occurrences = tabs.flatMap((tab) => tab.groups.flatMap((group) =>
      group.items.filter((item) => item.kind === 'action' && item.launcherKind === canonical)
        .map(() => ({ tab: tab.id, group: group.caption })),
    ));
    expect(occurrences).toEqual([{ tab: 'tables', group: 'TABLES' }]);
    expect(tabs.flatMap((tab) => tab.groups.flatMap((group) => group.items)))
      .not.toContainEqual(expect.objectContaining({ launcherKind: retired }));
  });

  it('resolves a valid tab/group placement into that group', () => {
    const baselineTab = resolveRibbonTabs([], EMPTY_CONTEXT)[0]!;
    const baselineGroup = baselineTab.groups[0]!;
    const tabs = resolveRibbonTabs(
      [
        template({
          tab: baselineTab.id,
          group: baselineGroup.caption,
          order: Number.MAX_SAFE_INTEGER,
        }),
      ],
      EMPTY_CONTEXT,
    );

    const action = tabs
      .find((tab) => tab.id === baselineTab.id)
      ?.groups.find((group) => group.caption === baselineGroup.caption)
      ?.items.find((item) => item.kind === 'action');
    expect(action).toMatchObject({ launcherKind: 'map.summarize', label: 'Summarize' });
    expect(tabs.some((tab) => tab.id === 'misc')).toBe(false);
  });

  it('routes a known tab with an invalid group to Misc and warns', () => {
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const knownTab = resolveRibbonTabs([], EMPTY_CONTEXT)[0]!;

    const tabs = resolveRibbonTabs(
      [template({ tab: knownTab.id, group: 'DOES NOT EXIST', order: 0 })],
      EMPTY_CONTEXT,
    );

    const misc = tabs.find((tab) => tab.id === 'misc');
    expect(misc?.groups.flatMap((group) => group.items)).toEqual([
      expect.objectContaining({ launcherKind: 'map.summarize', label: 'Summarize', primary: true }),
    ]);
    expect(warning).toHaveBeenCalledWith(expect.stringContaining('summarize'));
  });

  it('renders a placed unlisted native action once in its canonical group', () => {
    const baselineTab = resolveRibbonTabs([], EMPTY_CONTEXT)[0]!;
    const baselineGroup = baselineTab.groups[0]!;
    const kind = 'frisket.test.curated';
    const tabs = resolveRibbonTabs(
      [unlistedNativeTemplate(kind, {
        tab: baselineTab.id,
        group: baselineGroup.caption,
        order: Number.MAX_SAFE_INTEGER,
      })],
      EMPTY_CONTEXT,
    );

    const occurrences = tabs.flatMap((tab) =>
      tab.groups.flatMap((group) =>
        group.items.filter((item) => item.kind === 'action' && item.launcherKind === kind),
      ),
    );
    expect(occurrences).toHaveLength(1);
    expect(tabs.some((tab) => tab.id === 'misc')).toBe(false);
  });

  it('keeps an unplaced native action in Misc', () => {
    const kind = 'third.party.unplaced';
    const tabs = resolveRibbonTabs([unlistedNativeTemplate(kind)], EMPTY_CONTEXT);

    expect(
      tabs.find((tab) => tab.id === 'misc')?.groups.flatMap((group) => group.items),
    ).toEqual([
      expect.objectContaining({ kind: 'action', launcherKind: kind, primary: true }),
    ]);
  });
});

// The four resolve launchers join cluster in transform/RESOLVE via
// ACTION_PLACEMENTS, orders
// 1-4 behind cluster's order-0 primary.
describe('resolve/RESOLVE group placement', () => {
  it('places cluster (primary) then substitute/replace/combine/fill_missing', () => {
    const resolveEntries = [
      'resolve.substitute', 'resolve.replace', 'resolve.combine', 'resolve.fill_missing',
    ].map((kind) => syntheticActionCatalogEntry(kind, {
      ui_hints: {
        form: 'generated',
        category: 'resolve',
        semantic_controls: {},
        logical_outputs: [{ key: 'cleaned', column_type: 'text' }],
      },
    }));
    const templates = actionTemplatesFromCatalog(
      completeMappedActionCatalog({ stockEntries: [CLUSTER_ENTRY, ...resolveEntries] }),
    );
    const tabs = resolveRibbonTabs(templates, EMPTY_CONTEXT);
    const group = tabs
      .find((tab) => tab.id === 'transform')
      ?.groups.find((candidate) => candidate.caption === 'RESOLVE');
    const actions = (group?.items ?? []).filter((item) => item.kind === 'action');
    expect(actions.map((item) => (item.kind === 'action' ? item.launcherKind : null))).toEqual([
      'cluster.values',
      'resolve.substitute',
      'resolve.replace',
      'resolve.combine',
      'resolve.fill_missing',
    ]);
    expect(actions.filter((item) => item.primary).map((item) => (
      item.kind === 'action' ? item.launcherKind : null
    ))).toEqual(['cluster.values']);
  });
});

describe('documents metadata group placement', () => {
  it('places media metadata in its canonical group without a Misc fallback', () => {
    const templates = actionTemplatesFromCatalog(
      completeMappedActionCatalog({ stockEntries: [METADATA_ENTRY] }));
    const tabs = resolveRibbonTabs(templates, EMPTY_CONTEXT);
    const group = tabs
      .find((tab) => tab.id === 'media')
      ?.groups.find((candidate) => candidate.caption === 'DOCUMENTS');

    expect(group?.items).toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: 'action', launcherKind: 'media.extract_metadata' }),
    ]));
  });
});

describe('semantic action placement', () => {
  it('places generated actions under their single canonical launcher ids', () => {
    const templates = actionTemplatesFromCatalog(
      completeMappedActionCatalog({
        stockEntries: [
          syntheticActionCatalogEntry('map.template'),
          syntheticActionCatalogEntry('map.clean_dates'),
          syntheticActionCatalogEntry('map.to_geo_point'),
        ],
      }),
    );
    const launcherKinds = resolveRibbonTabs(templates, EMPTY_CONTEXT)
      .flatMap((tab) => tab.groups)
      .flatMap((group) => group.items)
      .flatMap((item) => item.kind === 'action' ? [item.launcherKind] : []);

    expect(launcherKinds.filter((kind) => kind === 'map.template')).toHaveLength(1);
    expect(launcherKinds.filter((kind) => kind === 'map.clean_dates')).toHaveLength(1);
    expect(launcherKinds.filter((kind) => kind === 'map.to_geo_point')).toHaveLength(1);
    expect(launcherKinds).not.toContain('template');
    expect(launcherKinds).not.toContain('clean_dates');
    expect(launcherKinds).not.toContain('to_geo_point');
  });

  it('keeps generated geo conversion in the Location group with its canonical icon', () => {
    const entry = syntheticActionCatalogEntry('map.to_geo_point');
    const templates = actionTemplatesFromCatalog(
      completeMappedActionCatalog({ stockEntries: [entry] }),
    );
    const item = resolveRibbonTabs(templates, EMPTY_CONTEXT)
      .find((tab) => tab.id === 'research')
      ?.groups.find((group) => group.caption === 'LOCATION')
      ?.items.find((candidate) => candidate.kind === 'action'
        && candidate.launcherKind === entry.kind);

    expect(item).toMatchObject({
      launcherKind: 'map.to_geo_point',
      label: 'To geo point',
      Icon: LocateFixed,
    });
  });

  it('discovers an unlisted generated action from backend metadata', () => {
    const entry = syntheticActionCatalogEntry('map.decorate', {
      title: 'Decorate text',
      description: 'Decorate each source value.',
      input_schema: {
        type: 'object',
        properties: { source: { type: 'string' } },
      },
      ui_hints: {
        form: 'generated',
        category: 'text',
        semantic_controls: { source: 'column' },
        logical_outputs: [{ key: 'decorated', column_type: 'text' }],
      },
    });
    const templates = actionTemplatesFromCatalog(
      completeMappedActionCatalog({ stockEntries: [entry] }),
    );
    const template = templates.find((candidate) => candidate.actionKind === entry.kind);
    const item = resolveRibbonTabs(templates, EMPTY_CONTEXT)
      .find((tab) => tab.id === 'analyze')
      ?.groups.find((group) => group.caption === 'ANALYZE')
      ?.items.find((candidate) => candidate.kind === 'action'
        && candidate.launcherKind === entry.kind);

    expect(template).toMatchObject({
      kind: entry.kind,
      name: entry.title,
      description: entry.description,
      actionCategory: 'text',
      generatedAction: true,
    });
    expect(item).toMatchObject({ label: entry.title, Icon: Braces });
  });

  it('keeps classification in Analyze and every translation entry point in Language', () => {
    const templates = actionTemplatesFromCatalog(completeMappedActionCatalog());
    const tabs = resolveRibbonTabs(templates, EMPTY_CONTEXT);
    const categorize = tabs
      .find((tab) => tab.id === 'analyze')
      ?.groups.find((candidate) => candidate.caption === 'ANALYZE');
    const convert = tabs
      .find((tab) => tab.id === 'transform')
      ?.groups.find((candidate) => candidate.caption === 'LANGUAGE');

    expect(categorize?.items).toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: 'action', launcherKind: 'map.classify' }),
    ]));
    expect((convert?.items ?? []).map((item) => (
      item.kind === 'action' ? item.launcherKind : item.command
    ))).toEqual(expect.arrayContaining([
      'map.translate',
      'translate-compare',
    ]));
  });
});

describe('PDF contextual action placement', () => {
  it('keeps the PDF-specific subset of actions that accept file columns', () => {
    const templates = actionTemplatesFromCatalog(
      completeMappedActionCatalog({ stockEntries: [METADATA_ENTRY, syntheticActionCatalogEntry('media.ocr'), syntheticActionCatalogEntry('media.video_frames'), syntheticActionCatalogEntry('map.find_visual_cuts'), syntheticActionCatalogEntry('map.find_topic_sections'), syntheticActionCatalogEntry('media.extract_pdf_tables'), syntheticActionCatalogEntry('media.to_markdown'), syntheticActionCatalogEntry('derive.transcript_segments'), syntheticActionCatalogEntry('temporal.extract_range'), syntheticActionCatalogEntry('derive.temporal_segments')] }));
    const tabs = resolveRibbonTabs(templates, { ...EMPTY_CONTEXT, columnTypes: ['file'] });
    const pdfActions = tabs
      .find((tab) => tab.id === 'ctx-pdf')
      ?.groups.flatMap((group) => group.items)
      .filter((item) => item.kind === 'action');

    expect(pdfActions?.map((item) => item.launcherKind)).toEqual([
      'media.ocr',
      'media.to_markdown',
      'media.extract_pdf_tables',
      'media.extract_metadata',
    ]);
    expect(pdfActions?.[0]).toMatchObject({ launcherKind: 'media.ocr', primary: true });
  });
});

describe('link contextual action placement', () => {
  it('promotes direct link actions while keeping Quick search in Research', () => {
    const templates = actionTemplatesFromCatalog(completeMappedActionCatalog({
      stockEntries: [CAPTURE_ENTRY, syntheticActionCatalogEntry('media.ytdlp_download'), syntheticActionCatalogEntry('media.fetch_url'), syntheticActionCatalogEntry('research.web_search')],
    }));
    const tabs = resolveRibbonTabs(templates, { ...EMPTY_CONTEXT, columnTypes: ['link'] });
    const linkActions = tabs
      .find((tab) => tab.id === 'ctx-link')
      ?.groups.flatMap((group) => group.items)
      .filter((item) => item.kind === 'action');
    const researchActions = tabs
      .find((tab) => tab.id === 'research')
      ?.groups.find((group) => group.caption === 'RESEARCH')
      ?.items.filter((item) => item.kind === 'action');

    expect(linkActions?.map((item) => item.launcherKind)).toEqual([
      'media.fetch_url',
      'web.capture_page',
      'media.ytdlp_download',
    ]);
    expect(linkActions?.[0]).toMatchObject({ launcherKind: 'media.fetch_url', primary: true });
    expect(researchActions?.map((item) => item.launcherKind)).toContain('research.web_search');
  });
});

describe('focused media action placement', () => {
  it('separates video editing, transcript work, documents and language into groups', () => {
    const templates = actionTemplatesFromCatalog(completeMappedActionCatalog());
    const tabs = resolveRibbonTabs(templates, EMPTY_CONTEXT);
    const identities = (id: string, caption: string) => tabs.find((tab) => tab.id === id)?.groups
      .filter((group) => group.caption === caption)
      .flatMap((group) => group.items)
      .map((item) => item.kind === 'action' ? item.launcherKind
        : item.kind === 'command' ? item.command : item.contributionId);
    expect(identities('media', 'VIDEO')).toEqual(expect.arrayContaining([
      'map.find_visual_cuts', 'temporal.extract_range', 'derive.temporal_segments',
    ]));
    expect(identities('media', 'TRANSCRIPTS')).toEqual(expect.arrayContaining([
      'media.transcribe', 'map.find_topic_sections', 'derive.transcript_segments',
      'transcribe-compare', 'topic-compare',
    ]));
    expect(identities('media', 'DOCUMENTS')).toEqual(expect.arrayContaining([
      'media.ocr', 'media.to_markdown', 'media.extract_pdf_tables', 'ocr-compare',
    ]));
    expect(identities('transform', 'LANGUAGE')).toContain('translate-compare');
  });
});

describe('temporal contextual actions', () => {
  const templates = actionTemplatesFromCatalog(
    completeMappedActionCatalog({ stockEntries: [METADATA_ENTRY, syntheticActionCatalogEntry('media.transcribe'), syntheticActionCatalogEntry('media.video_frames'), syntheticActionCatalogEntry('map.find_visual_cuts'), syntheticActionCatalogEntry('map.find_topic_sections'), syntheticActionCatalogEntry('media.extract_pdf_tables'), syntheticActionCatalogEntry('media.to_markdown'), syntheticActionCatalogEntry('derive.transcript_segments'), syntheticActionCatalogEntry('temporal.extract_range'), syntheticActionCatalogEntry('derive.temporal_segments')] }));

  it('offers Find visual cuts when the sheet has video', () => {
    const tabs = resolveRibbonTabs(templates, { ...EMPTY_CONTEXT, columnTypes: ['video'] });
    const videoTab = tabs.find((tab) => tab.id === 'ctx-video');

    expect(videoTab?.groups.flatMap((group) => group.items)).toEqual([
      expect.objectContaining({ launcherKind: 'media.transcribe', label: 'Transcribe', primary: true }),
      expect.objectContaining({ launcherKind: 'map.find_visual_cuts', label: 'Find visual cuts', primary: false }),
      expect.objectContaining({ launcherKind: 'temporal.extract_range', label: 'Extract range', primary: false }),
      expect.objectContaining({ launcherKind: 'derive.temporal_segments', label: 'Split into segments', primary: false }),
      expect.objectContaining({ launcherKind: 'media.video_frames', label: 'Video frames', primary: false }),
      expect.objectContaining({ launcherKind: 'media.extract_metadata', label: 'Extract metadata', primary: false }),
    ]);
    expect(tabs.some((tab) => tab.id === 'ctx-transcript')).toBe(false);
  });

  it('offers transcription, clipping, and metadata when the sheet has audio but no video', () => {
    const tabs = resolveRibbonTabs(templates, { ...EMPTY_CONTEXT, columnTypes: ['audio'] });
    const audioTab = tabs.find((tab) => tab.id === 'ctx-audio');

    expect(audioTab?.groups.flatMap((group) => group.items)).toEqual([
      expect.objectContaining({ launcherKind: 'media.transcribe', label: 'Transcribe', primary: true }),
      expect.objectContaining({ launcherKind: 'temporal.extract_range', label: 'Extract range', primary: false }),
      expect.objectContaining({ launcherKind: 'derive.temporal_segments', label: 'Split into segments', primary: false }),
      expect.objectContaining({ launcherKind: 'media.extract_metadata', label: 'Extract metadata', primary: false }),
    ]);
    expect(tabs.some((tab) => tab.id === 'ctx-video')).toBe(false);
    expect(audioTab?.groups.flatMap((group) => group.items).some((item) => (
      item.kind === 'action' && item.launcherKind === 'map.find_visual_cuts'
    ))).toBe(false);
  });

  it('offers transcript-specific tools and common text workflows for timestamped transcripts', () => {
    const tabs = resolveRibbonTabs(templates, {
      ...EMPTY_CONTEXT,
      columnTypes: ['timestamped_transcript'],
    });
    const transcriptTab = tabs.find((tab) => tab.id === 'ctx-transcript');

    expect(transcriptTab?.groups.flatMap((group) => group.items)).toEqual([
      expect.objectContaining({
        launcherKind: 'map.find_topic_sections',
        label: 'Find topic changes',
        primary: true,
      }),
      expect.objectContaining({
        launcherKind: 'derive.transcript_segments',
        label: 'Split transcript',
        primary: false,
      }),
      expect.objectContaining({ launcherKind: 'map.summarize', label: 'Summarize rows', primary: false }),
      expect.objectContaining({ launcherKind: 'map.translate', label: 'Translate rows', primary: false }),
      expect.objectContaining({ launcherKind: 'map.extract', label: 'Extract fields', primary: false }),
    ]);
    expect(tabs.some((tab) => tab.id === 'ctx-video')).toBe(false);
  });

  it('keeps the audited temporal workflows reachable across video and transcript contexts', () => {
    const tabs = resolveRibbonTabs(templates, {
      ...EMPTY_CONTEXT,
      columnTypes: ['video', 'timestamped_transcript'],
    });
    const contextualLaunchers = tabs
      .filter((tab) => tab.id === 'ctx-video' || tab.id === 'ctx-transcript')
      .flatMap((tab) => tab.groups)
      .flatMap((group) => group.items)
      .filter((item) => item.kind === 'action')
      .map((item) => item.kind === 'action' ? item.launcherKind : null);

    expect(contextualLaunchers).toEqual([
      'media.transcribe',
      'map.find_visual_cuts',
      'temporal.extract_range',
      'derive.temporal_segments',
      'media.video_frames',
      'media.extract_metadata',
      'map.find_topic_sections',
      'derive.transcript_segments',
      'map.summarize',
      'map.translate',
      'map.extract',
    ]);
  });
});
