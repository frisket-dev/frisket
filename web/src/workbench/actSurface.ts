// The Act region's action surface: the ribbon and menu bar render the SAME
// resolved tab model at two densities. Contextual
// tabs and plugin entries are adaptive promotions within that shared model,
// not a separate menu taxonomy. Two things drive the resolved array:
//
//  1. RIBBON_LAYOUT below — a pure visual skeleton (tabs, groups, captions,
//     and the handful of non-action commands each group always renders:
//     Import data…, Export data, OCR Compare…, …). No action kind is named
//     here — this is NOT a placement table.
//  2. ACTION_PLACEMENTS (web/src/actions/model.ts) — the single source of
//     truth for every launcher kind's {tab, group, order, primary}.
//     resolveRibbonTabs is the one canonical resolver; each renderer chooses
//     only its visual density, so their content cannot drift between renderers.
//
// Dedicated workflow helpers are filtered only from this navigation surface;
// their catalog entries and drawers remain available to the owning workflows.
// A launcher kind with NO placement (and no static/generated ActionTemplate
// at all) is simply not rendered. A kind that HAS a template but no valid
// ACTION_PLACEMENTS entry is not silently dropped — it lands in the Misc/Other
// ribbon tab + menu and logs a console warning once (warnUnassignedKinds), so a
// forgotten or stale placement is loud, not invisible.

import {
  AudioLines,
  Ampersand,
  Bot,
  Braces,
  CalendarClock,
  Camera,
  CircleHelp,
  Code,
  Combine,
  Download,
  Database,
  Eraser,
  FileArchive,
  FileSpreadsheet,
  FileOutput,
  Film,
  GitMerge,
  Languages,
  ListTree,
  LocateFixed,
  MapPin,
  Network,
  PaintBucket,
  Pilcrow,
  Puzzle,
  Replace,
  ReplaceAll,
  Regex,
  ScanFace,
  ScanText,
  Scale,
  Scissors,
  Search,
  Shapes,
  Table,
  TableCellsMerge,
  Tags,
  Text,
  Upload,
  Users,
  Video,
  Webhook,
  Rows3,
  type LucideIcon,
} from 'lucide-react';
import type { ActionTemplate } from '../api/types';
import { isStandaloneRibbonAction } from '../actions/registry';
import type { WorkbenchDataRequirement } from './descriptors';
import {
  firstMissingDataRequirement,
  type WorkbenchDataRequirementContext,
} from './dataRequirements';

/** Lucide glyph per launcher kind, translated to the app's icon set.
 *  Consistency over exactness. */
export const ACTION_ICON_BY_KIND: Record<string, LucideIcon> = {
  'media.ocr': ScanText,
  'media.to_markdown': Pilcrow,
  'media.extract_pdf_tables': Table,
  'map.find': Search,
  'map.extract': Braces,
  'map.mcp_extract': Webhook,
  'map.classify': Shapes,
  'derive.table_from_list': Combine,
  'map.ner': Tags,
  'map.ask': CircleHelp,
  'enrich.geocode': MapPin,
  'research.web_search': Search,
  'join.semantic': GitMerge,
  'derive.join': TableCellsMerge,
  'enrich.census_demographics': Users,
  'research.answer': Bot,
  'media.transcribe': AudioLines,
  'media.video_frames': Film,
  'media.extract_faces': ScanFace,
  'media.extract_metadata': FileArchive,
  'export.column_tables': FileOutput,
  'media.ytdlp_download': Video,
  'map.find_visual_cuts': Film,
  'map.find_topic_sections': Rows3,
  'derive.transcript_segments': Scissors,
  'temporal.extract_range': Scissors,
  'derive.temporal_segments': Rows3,
  'media.fetch_url': Download,
  'web.capture_page': Camera,
  'web.capture_screenshot': Camera,
  'map.summarize': Text,
  'map.translate': Languages,
  'map.judge': Scale,
  'reduce.group_summary': Combine,
  'map.regex_extract': Regex,
  'map.python': Code,
  'map.api_call': Webhook,
  'map.columns_from_json': ListTree,
  'cluster.values': Network,
  'resolve.substitute': ReplaceAll,
  'resolve.replace': Replace,
  'resolve.combine': Ampersand,
  'resolve.fill_missing': PaintBucket,
  'map.clean_column': Eraser,
  'map.to_geo_point': LocateFixed,
};

/** Short display labels. Falls back to the catalog template's name when a
 *  kind is not overridden here. */
const ACTION_LABEL_BY_KIND: Record<string, string> = {
  'media.ocr': 'OCR',
  'media.to_markdown': 'To Markdown',
  'media.extract_pdf_tables': 'PDF tables',
  'map.extract': 'Extract fields',
  'map.ask': 'Ask',
  'map.ner': 'Named entities',
  'enrich.geocode': 'Geocode',
  // Display names, not launcher-kind ids.
  'research.web_search': 'Quick search',
  'research.answer': 'Web research',
  'join.semantic': 'Semantic join',
  'derive.join': 'Join sheets (by key)',
  'enrich.census_demographics': 'Census',
  'media.ytdlp_download': 'Download media',
  'media.fetch_url': 'Fetch URL',
  'web.capture_page': 'Capture page',
  'web.capture_screenshot': 'Capture screenshot',
  'map.clean_dates': 'Clean dates',
  'map.clean_column': 'Clean column',
  'map.to_geo_point': 'To geo point',
  'media.video_frames': 'Video frames',
  'media.extract_faces': 'Extract faces',
  'media.extract_metadata': 'Extract metadata',
  'map.find_visual_cuts': 'Find visual cuts',
  'map.find_topic_sections': 'Find topic changes',
  'derive.transcript_segments': 'Split transcript',
  'temporal.extract_range': 'Extract range',
  'derive.temporal_segments': 'Split into segments',
};

interface RibbonCommandSpec {
  command: ActMenuCommand;
  label: string;
  icon: LucideIcon;
  primary?: boolean;
}

interface RibbonGroupLayout {
  caption: string;
  /** Non-action entries (import dialog, export modals, cluster panel) —
   *  always rendered, they don't depend on the action catalog. Actions for
   *  this (tab, caption) pair come from ACTION_PLACEMENTS, not from a list
   *  here. */
  commands?: readonly RibbonCommandSpec[];
}

interface RibbonTabLayout {
  id: string;
  label: string;
  groups: readonly RibbonGroupLayout[];
}

// The visual skeleton only — tab id doubles as the mirroring menu id. See
// the file header: no action kind is named below.
const RIBBON_LAYOUT: readonly RibbonTabLayout[] = [
  {
    id: 'data', label: 'Data', groups: [
      { caption: 'DATA', commands: [
        { command: 'import', label: 'Import data…', icon: Upload, primary: true },
        { command: 'sources', label: 'Sources & connections', icon: Database },
        { command: 'export-csv', label: 'Export data', icon: Download },
        { command: 'export-google-sheets', label: 'Google Sheets', icon: FileSpreadsheet },
        { command: 'export-column-tables', label: 'Tables as ZIP', icon: FileArchive },
      ] },
      { caption: 'WEB' },
    ],
  },
  {
    id: 'media', label: 'Media', groups: [
      { caption: 'DOCUMENTS', commands: [
        { command: 'ocr-compare', label: 'OCR Compare', icon: ScanText },
      ] },
      { caption: 'TRANSCRIPTS', commands: [
        { command: 'transcribe-compare', label: 'Transcribe Compare', icon: AudioLines },
        { command: 'topic-compare', label: 'Topic Compare', icon: Rows3 },
      ] },
      { caption: 'VIDEO' },
    ],
  },
  {
    id: 'analyze', label: 'Analyze', groups: [
      { caption: 'ANALYZE' },
      { caption: 'EXTRACT' },
      { caption: 'LIBRARY' },
    ],
  },
  {
    id: 'transform', label: 'Transform', groups: [
      { caption: 'TRANSFORM' },
      { caption: 'RESOLVE' },
      { caption: 'LANGUAGE', commands: [
        { command: 'translate-compare', label: 'Translate Compare', icon: Languages },
      ] },
    ],
  },
  { id: 'tables', label: 'Tables', groups: [{ caption: 'TABLES' }] },
  {
    id: 'research', label: 'Research', groups: [
      { caption: 'RESEARCH' },
      { caption: 'LOCATION' },
    ],
  },
];

/** Permanent tab identities, derived from the layout that both Act surfaces render. */
export const BASE_ACT_TAB_IDS: readonly string[] = RIBBON_LAYOUT.map((tab) => tab.id);

// Contextual tabs keyed to the presence of a column type via the SHARED
// dataRequirements helper (never a bespoke column-type check). PDF tools iff a
// `file` column exists; Link tools iff a `link` column exists. This is a
// DIFFERENT axis from ACTION_PLACEMENTS — an adaptive spotlight promotion of
// already-placed kinds onto a contextual tab, not their canonical home. Both
// densities receive it through the same resolved tab array.
//
// Membership is a curated subset of actions whose primary input accepts the
// triggering column type: promote the common, direct next steps without
// recreating every base tab. Keep the semantic subset explicit because `file`
// is shared by PDF, image, audio, and video actions; accepted types alone cannot
// distinguish a PDF tool. Whenever an action or input contract changes, audit
// all five lists together and pin the resolved contents in actSurface.test.ts.
interface ContextualGroupSpec {
  caption: string;
  primary?: string;
  actions?: readonly string[];
}

interface ContextualTabSpec {
  id: string;
  label: string;
  requirement: WorkbenchDataRequirement;
  accent: 'file' | 'link';
  groups: readonly ContextualGroupSpec[];
}

const CONTEXTUAL_TAB_SPECS: readonly ContextualTabSpec[] = [
  {
    id: 'ctx-pdf',
    label: 'PDF tools',
    accent: 'file',
    requirement: { kind: 'sheetHasColumnType', columnType: 'file' },
    groups: [
      { caption: 'PDF TOOLS', primary: 'media.ocr', actions: ['media.ocr', 'media.to_markdown', 'media.extract_pdf_tables', 'media.extract_metadata'] },
    ],
  },
  {
    id: 'ctx-link',
    label: 'Link tools',
    accent: 'link',
    requirement: { kind: 'sheetHasColumnType', columnType: 'link' },
    groups: [
      {
        caption: 'LINK TOOLS',
        primary: 'media.fetch_url',
        actions: ['media.fetch_url', 'web.capture_page', 'media.ytdlp_download'],
      },
    ],
  },
  {
    id: 'ctx-video',
    label: 'Video tools',
    accent: 'file',
    requirement: { kind: 'sheetHasColumnType', columnType: 'video' },
    groups: [
      {
        caption: 'VIDEO TOOLS',
        primary: 'media.transcribe',
        actions: [
          'media.transcribe',
          'map.find_visual_cuts',
          'temporal.extract_range',
          'derive.temporal_segments',
          'media.video_frames',
          'media.extract_metadata',
        ],
      },
    ],
  },
  {
    id: 'ctx-audio',
    label: 'Audio tools',
    accent: 'file',
    requirement: { kind: 'sheetHasColumnType', columnType: 'audio' },
    groups: [
      {
        caption: 'AUDIO TOOLS',
        primary: 'media.transcribe',
        actions: ['media.transcribe', 'temporal.extract_range', 'derive.temporal_segments', 'media.extract_metadata'],
      },
    ],
  },
  {
    id: 'ctx-transcript',
    label: 'Transcript tools',
    accent: 'file',
    requirement: { kind: 'sheetHasColumnType', columnType: 'timestamped_transcript' },
    groups: [
      {
        caption: 'TRANSCRIPT TOOLS',
        primary: 'map.find_topic_sections',
        actions: ['map.find_topic_sections', 'derive.transcript_segments', 'map.summarize', 'map.translate', 'map.extract'],
      },
    ],
  },
];

// Exactly the non-action command values the resolver can emit — every member
// appears in a RIBBON_LAYOUT group's `commands` list above. The eleven legacy
// members no renderer could ever emit ('export-root', view switching,
// toggles, row-height, saved-views, provenance, help-docs/help-shortcuts)
// were removed together with their runActCommand switch cases; their
// features live on via the toolbar/overflow controls, the WorkViewSwitcher,
// and the ⌘K palette.
export type ActMenuCommand =
  | 'import'
  | 'sources'
  | 'export-csv'
  | 'export-google-sheets'
  | 'export-column-tables'
  | 'ocr-compare'
  | 'transcribe-compare'
  | 'translate-compare'
  | 'topic-compare';

export type ResolvedActItem =
  | { kind: 'action'; launcherKind: string; label: string; Icon: LucideIcon; primary: boolean }
  | { kind: 'command'; command: ActMenuCommand; label: string; Icon: LucideIcon; primary: boolean }
  // A plugin launcher: host-owned reveal of a plugin panel / view's primary
  // placement. Ingested into the LIBRARY group.
  | {
      kind: 'launcher';
      contributionId: string;
      label: string;
      Icon: LucideIcon;
      primary: boolean;
    };

/** A plugin launcher to surface in the ribbon LIBRARY group. The click routes
 *  through the host's reveal handler (App.revealPluginLauncher), which reveals
 *  the descriptor's primary placement — no plugin code runs to render the
 *  launcher itself. */
export interface RibbonPluginLauncher {
  contributionId: string;
  label: string;
  iconName?: string;
}

// Descriptor icon NAME → glyph for launchers; unknown/absent falls back to the
// generic plugin glyph.
const LAUNCHER_ICON_BY_NAME: Record<string, LucideIcon> = {
  AudioLines,
  Bot,
  Braces,
  Combine,
  Download,
  Film,
  MapPin,
  Search,
  Shapes,
  Tags,
  Upload,
  Video,
};

function launcherIcon(iconName?: string): LucideIcon {
  return (iconName && LAUNCHER_ICON_BY_NAME[iconName]) || Puzzle;
}

export interface ResolvedActGroup {
  caption: string;
  items: ResolvedActItem[];
}

export interface ResolvedActTab {
  id: string;
  label: string;
  contextual: boolean;
  accent?: 'file' | 'link';
  groups: ResolvedActGroup[];
}

/** The DOM order shared by both densities: a group's promoted primary item
 *  precedes its remaining items, regardless of source-array insertion order. */
export function actItemsInDisplayOrder(
  items: readonly ResolvedActItem[],
): ResolvedActItem[] {
  return [...items.filter((item) => item.primary), ...items.filter((item) => !item.primary)];
}

function labelForKind(kind: string, template: ActionTemplate): string {
  return ACTION_LABEL_BY_KIND[kind] ?? template.name;
}

const ACTION_ICON_BY_CATEGORY: Record<string, LucideIcon> = {
  cleanup: CalendarClock,
  convert: LocateFixed,
  text: Braces,
};

function iconForKind(kind: string, template: ActionTemplate): LucideIcon {
  return ACTION_ICON_BY_KIND[kind]
    ?? (template.actionCategory ? ACTION_ICON_BY_CATEGORY[template.actionCategory] : undefined)
    ?? Puzzle;
}

// ---- Contextual-tab resolution (unchanged axis: literal kind lists) -------

function resolveContextualGroup(
  group: ContextualGroupSpec,
  byKind: Map<string, ActionTemplate>,
): ResolvedActGroup | null {
  const items: ResolvedActItem[] = [];
  for (const kind of group.actions ?? []) {
    const template = byKind.get(kind);
    if (!template) continue; // absent kinds simply not rendered
    items.push({
      kind: 'action',
      launcherKind: kind,
      label: labelForKind(kind, template),
      Icon: iconForKind(kind, template),
      primary: kind === group.primary,
    });
  }
  if (items.length === 0) return null;
  const firstPrimary = items.findIndex((item) => item.primary);
  items.forEach((item, index) => {
    item.primary = index === (firstPrimary === -1 ? 0 : firstPrimary);
  });
  return { caption: group.caption, items };
}

function templatesByLauncherKind(templates: readonly ActionTemplate[]): Map<string, ActionTemplate> {
  const byKind = new Map<string, ActionTemplate>();
  for (const template of templates) {
    if (!byKind.has(template.kind)) byKind.set(template.kind, template);
  }
  return byKind;
}

// ---- Single-source placement resolution (RIBBON_LAYOUT × ACTION_PLACEMENTS)

/** The base tab/group pairs RIBBON_LAYOUT declares — a placement naming any
 *  other tab or group (there shouldn't be one; ACTION_PLACEMENTS is hand-
 *  audited against this list) would otherwise silently vanish instead of
 *  falling back to Misc. */
const KNOWN_PLACEMENT_GROUPS = new Map<string, ReadonlySet<string>>(
  RIBBON_LAYOUT.map(
    (layout): [string, ReadonlySet<string>] => [
      layout.id,
      new Set(layout.groups.map((group) => group.caption)),
    ],
  ),
);

function hasValidPlacement(template: ActionTemplate): boolean {
  const placement = template.placement;
  return (
    placement !== undefined &&
    KNOWN_PLACEMENT_GROUPS.get(placement.tab)?.has(placement.group) === true
  );
}

function unassignedTemplates(templates: readonly ActionTemplate[]): ActionTemplate[] {
  return templates.filter((template) => !hasValidPlacement(template));
}

function unassignedItems(templates: readonly ActionTemplate[]): ResolvedActItem[] {
  const unassigned = unassignedTemplates(templates)
    .slice()
    .sort((a, b) => a.kind.localeCompare(b.kind));
  return unassigned.map((template, index) => ({
    kind: 'action',
    launcherKind: template.kind,
    label: labelForKind(template.kind, template),
    Icon: iconForKind(template.kind, template),
    primary: index === 0,
  }));
}

declare global {
  interface Window {
    /** e2e-only probe: a launcher kind name to inject as a synthetic,
     *  deliberately unplaced ActionTemplate so a spec can pin the Misc/Other
     *  fallback without depending on which real catalog kind happens to be
     *  unassigned. Read once per useWorkspaceModel catalog-load effect run
     *  (web/src/workspace/useWorkspaceModel.tsx); unset in production. */
    __FRISKET_TEST_UNASSIGNED_ACTION_KIND__?: string;
  }
}

/** e2e-only: appends a synthetic, deliberately-unplaced ActionTemplate when
 *  window.__FRISKET_TEST_UNASSIGNED_ACTION_KIND__ is set — a real e2e probe
 *  for the Misc/Other fallback without depending on whichever real catalog
 *  kinds happen to be unassigned. A no-op when the flag is unset. */
export function injectTestOnlyUnassignedAction(templates: ActionTemplate[]): ActionTemplate[] {
  const kind = typeof window === 'undefined' ? undefined : window.__FRISKET_TEST_UNASSIGNED_ACTION_KIND__;
  if (!kind || templates.some((template) => template.kind === kind)) return templates;
  return [
    ...templates,
    {
      kind: kind as ActionTemplate['kind'],
      name: 'Test Unassigned Action',
      description: 'e2e-only probe for the Misc/Other placement fallback (action-placement-single-source-v1).',
      defaultPrompt: '',
      defaultFields: [],
      noPrompt: true,
      noFields: true,
    },
  ];
}

const warnedUnassignedKinds = new Set<string>();

/** Any catalog kind with a template but no valid ACTION_PLACEMENTS entry lands
 *  in the shared Misc/Other tab (unassignedItems) instead of silently
 *  vanishing — and logs ONCE per kind per page session so a forgotten or stale
 *  placement is loud in dev, not invisible. */
function warnUnassignedKinds(templates: readonly ActionTemplate[]): void {
  for (const template of unassignedTemplates(templates)) {
    if (warnedUnassignedKinds.has(template.kind)) continue;
    warnedUnassignedKinds.add(template.kind);
    console.warn(
      `[actSurface] launcher kind "${template.kind}" has no valid ACTION_PLACEMENTS entry ` +
        '(web/src/actions/model.ts) — falling back to the shared Misc/Other tab ' +
        '(action-placement-single-source-v1).',
    );
  }
}

/** Resolves ONE (tab, group) pair's rendered items — the group's fixed
 *  commands, then its placed actions sorted by ACTION_PLACEMENTS order — and
 *  normalizes to exactly one primary item. Both densities consume these same
 *  groups; the compact renderer flattens them only for presentation. */
function resolvedItemsForGroup(
  group: RibbonGroupLayout,
  tabId: string,
  templates: readonly ActionTemplate[],
): ResolvedActItem[] {
  const items: ResolvedActItem[] = [];
  for (const command of group.commands ?? []) {
    items.push({
      kind: 'command',
      command: command.command,
      label: command.label,
      Icon: command.icon,
      primary: command.primary === true,
    });
  }
  const placed = templates
    .filter(
      (template) =>
        template.placement !== undefined &&
        template.placement.tab === tabId &&
        template.placement.group === group.caption,
    )
    .slice()
    .sort((a, b) => a.placement!.order - b.placement!.order);
  for (const template of placed) {
    items.push({
      kind: 'action',
      launcherKind: template.kind,
      label: labelForKind(template.kind, template),
      Icon: iconForKind(template.kind, template),
      primary: template.placement!.primary === true,
    });
  }
  if (items.length === 0) return items;
  // Guarantee exactly ONE primary tile even if the declared primary is absent
  // or two entries both claim it.
  const firstPrimary = items.findIndex((item) => item.primary);
  items.forEach((item, index) => {
    item.primary = index === (firstPrimary === -1 ? 0 : firstPrimary);
  });
  return items;
}

/** Builds the ribbon tab list from the live catalog + the sheet's column-type
 *  context. Base tabs always render (empty groups dropped); a Misc/Other tab
 *  appends when any non-plugin kind has no valid ACTION_PLACEMENTS entry; contextual
 *  tabs are appended only when their dataRequirement is satisfied. */
export function resolveRibbonTabs(
  templates: readonly ActionTemplate[],
  context: WorkbenchDataRequirementContext,
  pluginLaunchers: readonly RibbonPluginLauncher[] = [],
): ResolvedActTab[] {
  const ribbonTemplates = templates.filter((template) => isStandaloneRibbonAction(template.kind));
  warnUnassignedKinds(ribbonTemplates);
  const byKind = templatesByLauncherKind(ribbonTemplates);
  const tabs: ResolvedActTab[] = [];
  for (const layout of RIBBON_LAYOUT) {
    const groups: ResolvedActGroup[] = [];
    for (const group of layout.groups) {
      const items = resolvedItemsForGroup(group, layout.id, ribbonTemplates);
      if (items.length > 0) groups.push({ caption: group.caption, items });
    }
    if (groups.length === 0) continue;
    tabs.push({ id: layout.id, label: layout.label, contextual: false, groups });
  }
  // Plugin launchers ingest into the LIBRARY group in Analyze. LIBRARY carries no fixed command anymore
  // (the 'Custom action' tile was removed), so the group is dropped as empty by
  // the loop above — recreate it here on demand, then normalize the first
  // launcher to primary to preserve the one-primary-per-group invariant.
  if (pluginLaunchers.length > 0) {
    const analyzeTab = tabs.find((tab) => tab.id === 'analyze');
    if (analyzeTab) {
      let libraryGroup = analyzeTab.groups.find((group) => group.caption === 'LIBRARY');
      if (!libraryGroup) {
        libraryGroup = { caption: 'LIBRARY', items: [] };
        analyzeTab.groups.push(libraryGroup);
      }
      for (const launcher of pluginLaunchers) {
        libraryGroup.items.push({
          kind: 'launcher',
          contributionId: launcher.contributionId,
          label: launcher.label,
          Icon: launcherIcon(launcher.iconName),
          primary: false,
        });
      }
      const firstPrimary = libraryGroup.items.findIndex((item) => item.primary);
      libraryGroup.items.forEach((item, index) => {
        item.primary = index === (firstPrimary === -1 ? 0 : firstPrimary);
      });
    }
  }
  // Misc/Other fallback: any generated template ACTION_PLACEMENTS
  // doesn't cover. Dedicated non-ActionForm actions such as column-table
  // export are filtered before template generation and do not land here.
  const miscItems = unassignedItems(ribbonTemplates);
  if (miscItems.length > 0) {
    tabs.push({
      id: 'misc',
      label: 'Misc',
      contextual: false,
      groups: [{ caption: 'MISC', items: miscItems }],
    });
  }
  for (const spec of CONTEXTUAL_TAB_SPECS) {
    const unmet = firstMissingDataRequirement([spec.requirement], context);
    if (unmet !== null) continue; // column type absent → no contextual tab
    const groups = spec.groups
      .map((group) => resolveContextualGroup(group, byKind))
      .filter((group): group is ResolvedActGroup => group !== null);
    if (groups.length === 0) continue;
    tabs.push({ id: spec.id, label: spec.label, contextual: true, accent: spec.accent, groups });
  }
  return tabs;
}
