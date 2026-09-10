import type {
  ActionCatalogEntry,
  ActionCatalogPayload,
} from '../../src/api/types';
import { servedActionCatalog } from './servedActionCatalog';

function assertUniqueKinds(
  entries: readonly ActionCatalogEntry[],
  label: string,
): void {
  const seen = new Set<string>();
  for (const entry of entries) {
    if (seen.has(entry.kind)) {
      throw new Error(`${label} declares ${entry.kind} more than once`);
    }
    seen.add(entry.kind);
  }
}

/**
 * Small, structurally valid catalog declaration for one focused test.
 *
 * It deliberately claims only the metadata needed by the production
 * merge/presentation boundaries. Tests that consume richer schema or hint
 * behavior replace that one entry with an exact suite-local declaration.
 */
export function syntheticActionCatalogEntry(
  kind: string,
  overrides: Partial<ActionCatalogEntry> = {},
): ActionCatalogEntry {
  if (['web.capture_page', 'cluster.values', 'map.classify', 'map.extract', 'map.ner',
    'map.translate', 'map.mcp_extract', 'map.find', 'research.answer',
    'reduce.group_summary', 'derive.table_from_list'].includes(kind)) {
    const entry = servedActionCatalog().actions.find((item) => item.kind === kind);
    if (!entry) throw new Error(`Missing served action ${kind}`);
    return { ...structuredClone(entry), ...overrides };
  }
  let generated: Partial<ActionCatalogEntry> = {};
  if (kind === 'derive.join') {
    generated = {
      title: 'Join tables',
      input_schema: {
        type: 'object', additionalProperties: false, required: ['right', 'join_keys'],
        properties: {
          right: { type: 'object', additionalProperties: false, required: ['sheet_id'],
            properties: { sheet_id: { type: 'integer', minimum: 1 } } },
          join_keys: { type: 'array', minItems: 1, maxItems: 8, items: { type: 'object' } },
          how: { type: 'string', enum: ['inner', 'left', 'right', 'outer'], default: 'inner' },
          columns: { anyOf: [{ type: 'array', minItems: 1, items: { type: 'object' } }, { type: 'null' }], default: null },
          indicator: { type: 'boolean', default: false },
          max_output_rows: { type: 'integer', minimum: 1, default: 1_000_000 },
        },
      },
      row_scope_policy: { kind: 'sheet_rows', selectors: ['all_rows', 'exact_membership'] },
      writes_project: true,
      ui_hints: {
        form: 'generated', category: 'convert', semantic_controls: {},
        source_requirements: [], logical_outputs: [], dynamic_outputs: true,
        typed_action: { creates_sheet: true },
      },
    };
  } else if (kind === 'join.semantic') {
    generated = {
      title: 'Semantic join',
      input_schema: {
        type: 'object', additionalProperties: false, required: ['source', 'target'],
        properties: {
          source: { type: 'string' },
          target: { type: 'object', additionalProperties: false, required: ['sheet_id', 'column'],
            properties: { sheet_id: { type: 'integer', minimum: 1 }, column: { type: 'string' } } },
          carry: { type: 'array', items: { type: 'string' }, default: [] },
          match_threshold: { type: 'number', minimum: 0, maximum: 1, default: 0.7 },
          confident_threshold: { type: 'number', minimum: 0, maximum: 1, default: 0.85 },
        },
      },
      row_scope_policy: { kind: 'sheet_rows', selectors: ['all_rows', 'exact_membership'] },
      writes_project: true, async_mode: 'queued',
      required_capabilities: ['project:read', 'project:write', 'model:embed'],
      cost_policy: { kind: 'model_metered', requires_confirmation: true },
      ui_hints: {
        form: 'generated', category: 'convert', dynamic_outputs: true,
        semantic_controls: { source: 'column', carry: 'columns' },
        source_requirements: [{ id: 'source', mode: 'column', param: 'source', label: 'Source', min: 1, max: 1 }],
        logical_outputs: [{ key: 'match_value', column_type: 'text' },
          { key: 'match_score', column_type: 'number' }, { key: 'matched_row_id', column_type: 'integer' }],
        typed_action: { creates_sheet: true },
      },
    };
  } else if (kind === 'media.ytdlp_download') {
    generated = {
      title: 'Download media',
      input_schema: { type: 'object', additionalProperties: false, required: ['source'],
        properties: { source: { type: 'string' },
          media_type: { type: 'string', enum: ['audio', 'video'], default: 'video' },
          format_selector: { anyOf: [{ type: 'string' }, { type: 'null' }], default: null },
          extra_opts: { anyOf: [{ type: 'object' }, { type: 'null' }], default: null },
        } },
      required_capabilities: ['project:read', 'project:write', 'external:media_download'],
      row_scope_policy: { kind: 'sheet_rows', selectors: ['all_rows', 'exact_membership'] },
      writes_project: true, async_mode: 'queued',
      ui_hints: { form: 'generated', category: 'sources', dynamic_outputs: true,
        semantic_controls: { source: 'column' },
        logical_outputs: [{ key: 'video', column_type: 'video' }],
        source_requirements: [{ id: 'source', mode: 'column', param: 'source',
          label: 'Media URL', min: 1, max: 1, accepted_column_types: ['link', 'text'] }],
      },
    };
  } else if (kind === 'temporal.extract_range' || kind === 'derive.temporal_segments') {
    const split = kind === 'derive.temporal_segments';
    generated = {
      title: split ? 'Split into segments' : 'Extract range',
      input_schema: { type: 'object', additionalProperties: false, required: ['source', 'selection'],
        properties: { source: { type: 'string' }, selection: { type: 'object' } } },
      required_capabilities: ['project:read', 'project:write'],
      row_scope_policy: { kind: 'sheet_rows', selectors: ['all_rows', 'exact_membership'] },
      writes_project: true, async_mode: 'queued',
      ui_hints: { form: 'generated', category: 'convert', dynamic_outputs: true,
        ...(split ? { typed_action: { creates_sheet: true } } : {}),
        semantic_controls: { source: 'column', selection: 'transcript_selection' },
        logical_outputs: [{ key: 'clip', column_type: 'video' },
          ...(split ? [{ key: 'source_range', column_type: 'timeline_range' }] : [])],
        source_requirements: [{ id: 'source', mode: 'column', param: 'source',
          label: 'Audio or video', min: 1, max: 1, accepted_column_types: ['audio', 'video'] }],
      },
    };
  } else if (['media.fetch_url', 'web.capture_screenshot', 'media.video_frames', 'media.extract_faces'].includes(kind)) {
    const video = kind === 'media.video_frames';
    const faces = kind === 'media.extract_faces';
    const screenshot = kind === 'web.capture_screenshot';
    const output = video ? 'frames' : faces ? 'faces' : screenshot ? 'screenshot' : 'media';
    generated = {
      title: video ? 'Video frames' : faces ? 'Extract faces' : screenshot ? 'Capture screenshot' : 'Fetch URL',
      input_schema: { type: 'object', additionalProperties: false, required: ['source'],
        properties: { source: { type: 'string' } } },
      ui_hints: { form: 'generated', category: video || faces ? 'extract' : 'sources',
        semantic_controls: { source: 'column' },
        logical_outputs: [{ key: output, column_type: video || faces ? 'json' : screenshot ? 'image' : 'file' }],
        source_requirements: [{ id: 'source', mode: 'column', param: 'source', label: 'Source', min: 1,
          accepted_column_types: video ? ['video', 'file'] : faces ? ['image', 'file'] : ['link', 'text'] }],
      },
    };
  } else if (kind === 'media.ocr' || kind === 'media.transcribe') {
    const ocr = kind === 'media.ocr';
    generated = {
      title: ocr ? 'OCR' : 'Transcribe',
      input_schema: { type: 'object', additionalProperties: false, required: ['source'],
        properties: { source: { type: 'string' }, engine: { type: 'string', default: ocr ? 'rapidocr' : 'faster_whisper' },
          ...(ocr ? { language: { type: 'string', default: null }, dpi: { type: 'integer', default: 200 },
            searchable_pdf: { type: 'boolean', default: false } } : {}),
        } },
      ui_hints: { form: 'generated', category: 'extract', semantic_controls: { source: 'column', engine: 'engine' },
        logical_outputs: [{ key: 'text', column_type: ocr ? 'text' : 'timestamped_transcript' },
          { key: ocr ? 'blocks' : 'segments', column_type: 'json' }],
        source_requirements: [{ id: 'source', mode: 'column', param: 'source', label: 'Media', min: 1,
          accepted_column_types: ocr ? ['image', 'file'] : ['audio', 'video', 'file'] }],
      },
    };
  } else if (kind === 'media.to_markdown') {
    generated = {
      title: 'To Markdown',
      input_schema: { type: 'object', additionalProperties: false, required: ['source'],
        properties: { source: { type: 'string' }, engine: { type: 'string', default: 'markitdown' } } },
      ui_hints: { form: 'generated', category: 'extract', semantic_controls: { source: 'column', engine: 'engine' },
        logical_outputs: [{ key: 'markdown', column_type: 'text' }],
        source_requirements: [{ id: 'source', mode: 'column', param: 'source', label: 'Document', min: 1,
          accepted_column_types: ['file', 'text'] }],
      },
    };
  } else if (['map.find_visual_cuts', 'map.find_topic_sections', 'media.extract_pdf_tables', 'derive.transcript_segments'].includes(kind)) {
    const topic = kind === 'map.find_topic_sections';
    const pdf = kind === 'media.extract_pdf_tables';
    const transcript = kind === 'derive.transcript_segments';
    generated = {
      title: pdf ? 'Extract PDF tables' : transcript ? 'Split transcript' : topic ? 'Find topic changes' : 'Find visual cuts',
      input_schema: {
        type: 'object', additionalProperties: false, required: ['source'],
        properties: { source: { type: 'string', title: 'Source' } },
      },
      required_capabilities: ['project:read', 'project:write'],
      execution_mode: 'per_row', async_mode: 'queued', writes_project: true,
      ui_hints: {
        form: 'generated', category: 'extract', semantic_controls: { source: 'column' },
        logical_outputs: [{ key: pdf ? 'pdf_tables' : transcript ? 'transcript' : topic ? 'sections' : 'cuts',
          column_type: pdf ? 'json' : transcript ? 'timestamped_transcript' : topic ? 'timeline_ranges' : 'timeline_points' }],
        ...(transcript ? { typed_action: { creates_sheet: true } } : {}),
        source_requirements: [{
          id: 'source', mode: 'column', param: 'source', label: 'Video', min: 1,
          accepted_column_types: [pdf ? 'file' : topic || transcript ? 'timestamped_transcript' : 'video'],
        }],
      },
    };
  } else if (kind === 'map.template') {
    generated = {
        input_schema: {
          type: 'object',
          additionalProperties: false,
          required: ['template'],
          properties: { template: { type: 'object', title: 'Template', additionalProperties: false,
            required: ['text'], properties: { text: { type: 'string' } } } },
        },
        ui_hints: {
          form: 'generated',
          category: 'text',
          semantic_controls: { template: 'template' },
          source_requirements: [{
            id: 'template',
            mode: 'template',
            param: 'template',
            label: 'Template',
            min: 0,
            template_columns: 'union',
          }],
          logical_outputs: [{ key: 'rendered', column_type: 'text' }],
        },
      };
  } else if (kind === 'map.clean_column') {
    generated = {
      input_schema: {
        type: 'object',
        additionalProperties: false,
        required: ['input_column'],
        properties: {
          input_column: { type: 'string', title: 'Input column' },
          transformations: { type: 'array', default: [] },
        },
      },
      ui_hints: {
        form: 'generated',
        category: 'cleanup',
        semantic_controls: { input_column: 'column' },
        source_requirements: [{
          id: 'input_column',
          mode: 'column',
          param: 'input_column',
          label: 'Input column',
          min: 1,
        }],
        logical_outputs: [{ key: 'cleaned', column_type: 'text' }],
      },
    };
  } else if (kind === 'map.clean_dates') {
    generated = {
          input_schema: {
            type: 'object',
            additionalProperties: false,
            required: ['source'],
            properties: {
              source: { type: 'string', title: 'Source' },
              format: { default: null, title: 'Format' },
            },
          },
          ui_hints: {
            form: 'generated',
            category: 'cleanup',
            semantic_controls: { source: 'column' },
            source_requirements: [{
              id: 'source',
              mode: 'column',
              param: 'source',
              label: 'Source',
              min: 1,
              accepted_column_types: ['text'],
            }],
            logical_outputs: [{ key: 'cleaned', column_type: 'date' }],
          },
        };
  } else if (kind === 'map.to_geo_point') {
    generated = {
      input_schema: {
        type: 'object',
        additionalProperties: false,
        required: ['latitude_column', 'longitude_column'],
        properties: {
          latitude_column: { type: 'string', title: 'Latitude column' },
          longitude_column: { type: 'string', title: 'Longitude column' },
        },
      },
      ui_hints: {
        form: 'generated',
        category: 'convert',
        semantic_controls: {
          latitude_column: 'column',
          longitude_column: 'column',
        },
        source_requirements: [
          {
            id: 'latitude_column',
            mode: 'column',
            param: 'latitude_column',
            label: 'Latitude column',
            min: 1,
            accepted_column_types: ['integer', 'number', 'text'],
          },
          {
            id: 'longitude_column',
            mode: 'column',
            param: 'longitude_column',
            label: 'Longitude column',
            min: 1,
            accepted_column_types: ['integer', 'number', 'text'],
          },
        ],
        logical_outputs: [{ key: 'geo_point', column_type: 'geo_point' }],
      },
    };
  } else if (kind === 'research.web_search') {
    generated = {
      title: 'Quick search',
      input_schema: {
        type: 'object',
        additionalProperties: false,
        required: ['query'],
        properties: {
          query: { type: 'object', title: 'Query', additionalProperties: false,
            required: ['text'], properties: { text: { type: 'string' } } },
          max_results: { type: 'integer', default: 10, minimum: 1, maximum: 20 },
        },
      },
      output_schema: {
        type: 'object',
        properties: { search_results: { type: 'array' } },
      },
      cost_policy: { kind: 'external_metered', requires_confirmation: true },
      ui_hints: {
        form: 'generated',
        category: 'sources',
        semantic_controls: { query: 'template' },
        source_requirements: [{
          id: 'query',
          mode: 'template',
          param: 'query',
          label: 'Query',
          min: 0,
          template_columns: 'union',
        }],
        logical_outputs: [{ key: 'search_results', column_type: 'json' }],
      },
    };
  } else if (kind === 'map.columns_from_json') {
    generated = {
      input_schema: {
        type: 'object',
        additionalProperties: false,
        required: ['source_column', 'routes'],
        properties: {
          source_column: { type: 'string', title: 'JSON source' },
          routes: { type: 'array', minItems: 1, items: { type: 'object' } },
        },
      },
      ui_hints: {
        form: 'generated',
        category: 'extract',
        semantic_controls: { source_column: 'column' },
        source_requirements: [{
          id: 'source_column',
          mode: 'column',
          param: 'source_column',
          label: 'JSON source',
          min: 1,
          accepted_column_types: ['json'],
        }],
        logical_outputs: [],
        dynamic_outputs: true,
      },
    };
  }
  return {
    kind,
    authoring_contract_version: 1,
    title: kind,
    description: `Synthetic ${kind} catalog entry`,
    input_schema: {
      type: 'object',
      additionalProperties: false,
      properties: {},
    },
    output_schema: { type: 'object', properties: {} },
    errors: [],
    side_effects: [],
    required_capabilities: [],
    required_credentials: [],
    cost_policy: {},
    idempotency: {},
    retry_policy: {},
    execution_mode: 'per_row',
    async_mode: 'sync',
    writes_project: false,
    examples: [],
    ui_hints: {
      default_output: { name: 'result', type: 'text' },
    },
    receipt_policy: 'writes_receipt',
    ...generated,
    ...overrides,
  };
}

export interface CompleteMappedActionCatalogOptions {
  /** Exact replacements for canonical kinds in the production mapped registry. */
  replacements?: readonly ActionCatalogEntry[];
  /** Explicit non-renderable stock entries needed by a focused test. */
  stockEntries?: readonly ActionCatalogEntry[];
}

/**
 * Complete mapped catalog for focused frontend tests.
 *
 * Completeness and ordering come from the served catalog fixture. Focused
 * tests may replace one served declaration or append an unrelated stock
 * declaration without maintaining a second roster.
 */
export function completeMappedActionCatalog(
  options: CompleteMappedActionCatalogOptions = {},
): ActionCatalogPayload {
  const replacements = options.replacements ?? [];
  const stockEntries = options.stockEntries ?? [];
  assertUniqueKinds(replacements, 'catalog replacements');
  assertUniqueKinds(stockEntries, 'catalog stock entries');

  const served = servedActionCatalog();
  const servedKinds = new Set(served.actions.map((entry) => entry.kind));
  const replacementsByKind = new Map<string, ActionCatalogEntry>();
  for (const entry of replacements) {
    if (!servedKinds.has(entry.kind)) {
      throw new Error(
        `catalog replacement ${entry.kind} is not a served action; `
        + 'declare it as a stock entry',
      );
    }
    replacementsByKind.set(entry.kind, entry);
  }

  const appendedStockEntries: ActionCatalogEntry[] = [];
  for (const entry of stockEntries) {
    if (servedKinds.has(entry.kind)) {
      if (replacementsByKind.has(entry.kind)) {
        throw new Error(`catalog declares ${entry.kind} more than once`);
      }
      replacementsByKind.set(entry.kind, entry);
      continue;
    }
    appendedStockEntries.push(entry);
  }

  const actions = served.actions.map((entry) => (
    replacementsByKind.get(entry.kind) ?? structuredClone(entry)
  ));
  const catalogKinds = new Set(actions.map((entry) => entry.kind));
  for (const entry of appendedStockEntries) {
    if (catalogKinds.has(entry.kind)) {
      throw new Error(`catalog declares ${entry.kind} more than once`);
    }
    catalogKinds.add(entry.kind);
    actions.push(entry);
  }

  return {
    schema_version: 'frisket.action_catalog.v2',
    actions,
    action_schema: {},
    error_schema: {},
    result_schema: {},
    receipt_schema: {},
    validation_result_schema: {},
  };
}
