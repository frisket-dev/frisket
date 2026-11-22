import { httpContract, type HttpContractSuccessResponse, type RequestOptions } from './httpContract';
import type {
  CitationViewerReadOptions,
  TextAnnotationsOptions,
  TextAnnotationLayer,
  TextAnnotations,
  TextAnnotationSpan,
  TextAnnotationUnpositionedReason,
} from './types';
import { isRecord } from './wireValues';

type CitationViewerOptions = CitationViewerReadOptions;

type CellCitationsWire =
  HttpContractSuccessResponse<'tenant.cell_citations.get'>;
type CellTextAnnotationsWire =
  HttpContractSuccessResponse<'tenant.cell_text_annotations.get'>;
type ColumnCitationsWire =
  HttpContractSuccessResponse<'tenant.column_citations.get'>;
type CitationViewerWire =
  HttpContractSuccessResponse<'tenant.citation_viewer.get'>;
type RowSourceDocumentsWire =
  HttpContractSuccessResponse<'tenant.row_source_documents.get'>;

// The public API keeps numeric route references as strings. The generated
// route signatures reflect FastAPI's integers; these casts preserve the
// existing URL bytes without adding a second runtime representation or validator.
const numericPathRef = (value: string): number => value as unknown as number;

const TEXT_ANNOTATION_UNPOSITIONED_REASONS: readonly TextAnnotationUnpositionedReason[] = [
  'content_hash_mismatch',
  'unsupported_offset_unit',
  'invalid_geometry',
];

function isUnpositionedReason(value: unknown): value is TextAnnotationUnpositionedReason {
  return TEXT_ANNOTATION_UNPOSITIONED_REASONS.includes(
    value as TextAnnotationUnpositionedReason,
  );
}

/** Wire -> client for one annotation layer. Drops a layer whose shape it
 * cannot read rather than inventing one: a layer that reaches the reader
 * malformed would either draw marks at guessed offsets or claim a dirty state
 * nothing measured. `flatMap` over this is the same "drop, do not adapt"
 * discipline the mentions groups use for an unparseable selector. */
function mapTextAnnotationLayer(raw: unknown): TextAnnotationLayer[] {
  if (!isRecord(raw)) return [];
  const toggleKey = typeof raw.toggle_key === 'string' ? raw.toggle_key : '';
  const layerFamily = typeof raw.layer_family === 'string' ? raw.layer_family : '';
  if (!toggleKey || !layerFamily) return [];
  const producer = isRecord(raw.producer) ? raw.producer : {};
  const generatedColumn = isRecord(raw.generated_column) ? raw.generated_column : {};
  const common = {
    toggleKey,
    layerFamily,
    producerKind: typeof producer.kind === 'string' ? producer.kind : null,
    producerEngine: typeof producer.engine === 'string' ? producer.engine : null,
    generatedColumn: {
      id: String(generatedColumn.id ?? ''),
      name: typeof generatedColumn.name === 'string' ? generatedColumn.name : null,
    },
  };
  if (raw.positioned === true) {
    const counts = isRecord(raw.counts) ? raw.counts : {};
    const spans: TextAnnotationSpan[] = Array.isArray(raw.spans)
      ? raw.spans.flatMap((span) => {
        if (!isRecord(span)) return [];
        const start = Number(span.start);
        const end = Number(span.end);
        // A non-finite or inverted range is not renderable geometry; the
        // partition would silently misplace every later mark.
        if (!Number.isInteger(start) || !Number.isInteger(end) || start >= end) return [];
        const metadata = isRecord(span.metadata) ? span.metadata : {};
        return [{
          occurrenceId: String(span.occurrence_id ?? `${start}:${end}`),
          ...(typeof span.source_start === 'number' ? { sourceStart: span.source_start } : {}),
          ...(typeof span.source_end === 'number' ? { sourceEnd: span.source_end } : {}),
          ...(span.quote_truncated === true ? { quoteTruncated: true } : {}),
          start,
          end,
          quote: String(span.quote ?? ''),
          entityType: typeof metadata.entity_type === 'string' ? metadata.entity_type : null,
          entityFingerprint:
            typeof metadata.entity_fingerprint === 'string' && metadata.entity_fingerprint
              ? metadata.entity_fingerprint
              : null,
        }];
      })
      : [];
    return [{
      ...common,
      positioned: true,
      spans,
      counts: {
        shown: Number(counts.shown ?? spans.length),
        total: Number(counts.total ?? spans.length),
        invalid: Number(counts.invalid ?? 0),
      },
    }];
  }
  const unpositioned = isRecord(raw.unpositioned) ? raw.unpositioned : {};
  const reason = isUnpositionedReason(unpositioned.reason)
    ? unpositioned.reason
    : 'invalid_geometry';
  return [{
    ...common,
    positioned: false,
    unpositioned: { reason, total: Number(unpositioned.total ?? 0) },
  }];
}

function textAnnotationsFromWire(
  wire: CellTextAnnotationsWire,
  rowId: string,
  columnId: string,
): TextAnnotations {
  return {
    sheetId: String(wire.sheet_id ?? ''),
    rowId: String(wire.row_id ?? rowId),
    columnId: String(wire.column_id ?? columnId),
    // `null` is meaningful here (no renderable text), so it is preserved
    // rather than coerced to ''.
    text: typeof wire.text === 'string' ? wire.text : null,
    ...(wire.window ? { window: wire.window } : {}),
    contentHash: typeof wire.content_hash === 'string' ? wire.content_hash : null,
    layers: Array.isArray(wire.layers) ? wire.layers.flatMap(mapTextAnnotationLayer) : [],
  };
}

/** Product-facing evidence reads. The three open JSON payloads stay exactly
 * as the generated transport returns them; annotations alone have a client
 * domain model because their geometry is rendered directly. */
export function createProjectCitationsApi(
  projectId: string,
) {
  return {
    /** Existing citations and readable original documents reachable through a
     * bounded, cycle-safe row-ancestry walk. Fetched only for the open row. */
    getRowSourceDocuments(rowId: string, options: RequestOptions = {}): Promise<RowSourceDocumentsWire> {
      return httpContract(
        'tenant.row_source_documents.get',
        {
          pathParams: { pid: projectId, row_id: numericPathRef(rowId) },
          signal: options.signal,
        },
      );
    },
    getCellCitations(
      rowId: string,
      columnId: string,
      options: RequestOptions & { includeStale?: boolean } = {},
    ): Promise<CellCitationsWire> {
      return httpContract(
        'tenant.cell_citations.get',
        {
          pathParams: {
            pid: projectId,
            row_id: numericPathRef(rowId),
            column_id: numericPathRef(columnId),
          },
          query: {
            // The existing transport emits `include_stale=1`; keep that byte
            // shape while satisfying the generated boolean query signature.
            include_stale: options.includeStale ? (1 as unknown as boolean) : undefined,
          },
          signal: options.signal,
        },
      );
    },

    /** The annotation layers whose coordinate surface is this cell. Fetched
     * lazily for the active cell only; a cell with no layers resolves with
     * `layers: []` rather than throwing. */
    async getTextAnnotations(
      rowId: string,
      columnId: string,
      options: TextAnnotationsOptions = {},
    ): Promise<TextAnnotations> {
      const wire = await httpContract(
        'tenant.cell_text_annotations.get',
        {
          pathParams: {
            pid: projectId,
            row_id: numericPathRef(rowId),
            column_id: numericPathRef(columnId),
          },
          query: { start: options.start, limit: options.limit, revision: options.revision, occurrence_id: options.occurrenceId },
          signal: options.signal,
        },
      );
      return textAnnotationsFromWire(wire, rowId, columnId);
    },

    /** One call per (sheet, column) instead of one cell-citations call per
     * visible row. `rowIds` scopes the batch to the list's virtualization
     * window; omitted = every row on the sheet. */
    getColumnCitations(
      sheetId: string,
      columnId: string,
      options: RequestOptions & { rowIds?: readonly string[] } = {},
    ): Promise<ColumnCitationsWire> {
      return httpContract(
        'tenant.column_citations.get',
        {
          pathParams: {
            pid: projectId,
            sheet_id: numericPathRef(sheetId),
            column_id: numericPathRef(columnId),
          },
          query: { row_ids: options.rowIds?.length ? options.rowIds.join(',') : undefined },
          signal: options.signal,
        },
      );
    },

    getCitationViewer(
      citationId: string | number,
      options: CitationViewerOptions = {},
    ): Promise<CitationViewerWire> {
      return httpContract(
        'tenant.citation_viewer.get',
        {
          pathParams: {
            pid: projectId,
            citation_id: String(citationId),
          },
          query: {
            locate_current: options.locateCurrent ? true : undefined,
            text_artifact_id: options.textArtifactId,
            text_start: options.textStart,
            text_limit: options.textLimit,
            text_revision: options.textRevision,
            text_span_id: options.textSpanId,
          },
          signal: options.signal,
        },
      );
    },
  };
}
