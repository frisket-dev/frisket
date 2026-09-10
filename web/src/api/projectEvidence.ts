import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import type {
  TextAnnotationLayer,
  TextAnnotations,
  TextAnnotationSpan,
  TextAnnotationUnpositionedReason,
} from './types';

export interface ProjectEvidenceOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export type CellEvidenceWire =
  HttpContractSuccessResponse<'tenant.cell_evidence.get'>;
export type CellTextAnnotationsWire =
  HttpContractSuccessResponse<'tenant.cell_text_annotations.get'>;
export type ColumnEvidenceWire =
  HttpContractSuccessResponse<'tenant.column_evidence.get'>;
export type EvidenceViewerWire =
  HttpContractSuccessResponse<'tenant.evidence_viewer.get'>;

export interface ProjectEvidenceApi {
  getCellEvidence(
    rowId: string,
    columnId: string,
    includeStale?: boolean,
    options?: ProjectEvidenceOptions,
  ): Promise<CellEvidenceWire>;
  getTextAnnotations(
    rowId: string,
    columnId: string,
    options?: ProjectEvidenceOptions,
  ): Promise<CellTextAnnotationsWire>;
  getColumnEvidence(
    sheetId: string,
    columnId: string,
    rowIds?: readonly string[],
    options?: ProjectEvidenceOptions,
  ): Promise<ColumnEvidenceWire>;
  getEvidenceViewer(
    evidenceLinkId: string | number,
    options?: ProjectEvidenceOptions,
  ): Promise<EvidenceViewerWire>;
}

/** Product-facing evidence reads. The three open JSON payloads stay exactly
 * as the generated transport returned them; annotations alone have a client
 * domain model because their geometry is rendered directly. */
export interface ProjectEvidenceDomainApi {
  getCellEvidence(
    rowId: string,
    columnId: string,
    includeStale?: boolean,
    options?: ProjectEvidenceOptions,
  ): Promise<CellEvidenceWire>;
  getTextAnnotations(
    rowId: string,
    columnId: string,
    options?: ProjectEvidenceOptions,
  ): Promise<TextAnnotations>;
  getColumnEvidence(
    sheetId: string,
    columnId: string,
    rowIds?: readonly string[],
    options?: ProjectEvidenceOptions,
  ): Promise<ColumnEvidenceWire>;
  getEvidenceViewer(
    evidenceLinkId: string | number,
    options?: ProjectEvidenceOptions,
  ): Promise<EvidenceViewerWire>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;

// FrisketApi keeps numeric route references as strings. The generated route
// signatures reflect FastAPI's integers; these casts preserve the existing URL
// bytes without adding a second runtime representation or validator.
const numericPathRef = (value: string): number => value as unknown as number;

export function createProjectEvidenceApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): ProjectEvidenceApi {
  return {
    getCellEvidence(rowId, columnId, includeStale = false, options = {}) {
      return httpContract(
        'tenant.cell_evidence.get',
        {
          pathParams: {
            pid: projectId,
            row_id: numericPathRef(rowId),
            column_id: numericPathRef(columnId),
          },
          query: {
            // The existing transport emits `include_stale=1`; keep that byte
            // shape while satisfying the generated boolean query signature.
            include_stale: includeStale ? (1 as unknown as boolean) : undefined,
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    getTextAnnotations(rowId, columnId, options = {}) {
      return httpContract(
        'tenant.cell_text_annotations.get',
        {
          pathParams: {
            pid: projectId,
            row_id: numericPathRef(rowId),
            column_id: numericPathRef(columnId),
          },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    getColumnEvidence(sheetId, columnId, rowIds, options = {}) {
      return httpContract(
        'tenant.column_evidence.get',
        {
          pathParams: {
            pid: projectId,
            sheet_id: numericPathRef(sheetId),
            column_id: numericPathRef(columnId),
          },
          query: { row_ids: rowIds?.length ? rowIds.join(',') : undefined },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    getEvidenceViewer(evidenceLinkId, options = {}) {
      return httpContract(
        'tenant.evidence_viewer.get',
        {
          pathParams: {
            pid: projectId,
            evidence_link_id: String(evidenceLinkId),
          },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}

const TEXT_ANNOTATION_UNPOSITIONED_REASONS: readonly TextAnnotationUnpositionedReason[] = [
  'content_hash_mismatch',
  'unsupported_offset_unit',
  'invalid_geometry',
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

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
  const outputColumn = isRecord(raw.output_column) ? raw.output_column : {};
  const common = {
    toggleKey,
    layerFamily,
    producerKind: typeof producer.kind === 'string' ? producer.kind : null,
    producerEngine: typeof producer.engine === 'string' ? producer.engine : null,
    outputColumn: {
      id: String(outputColumn.id ?? ''),
      name: typeof outputColumn.name === 'string' ? outputColumn.name : null,
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
    contentHash: typeof wire.content_hash === 'string' ? wire.content_hash : null,
    layers: Array.isArray(wire.layers) ? wire.layers.flatMap(mapTextAnnotationLayer) : [],
  };
}

export function createProjectEvidenceDomainApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): ProjectEvidenceDomainApi {
  const evidenceApi = createProjectEvidenceApi(errorFactory, projectId);
  return {
    getCellEvidence(rowId, columnId, includeStale, options) {
      return evidenceApi.getCellEvidence(rowId, columnId, includeStale, options);
    },
    async getTextAnnotations(rowId, columnId, options) {
      return textAnnotationsFromWire(
        await evidenceApi.getTextAnnotations(rowId, columnId, options),
        rowId,
        columnId,
      );
    },
    getColumnEvidence(sheetId, columnId, rowIds, options) {
      return evidenceApi.getColumnEvidence(sheetId, columnId, rowIds, options);
    },
    getEvidenceViewer(evidenceLinkId, options) {
      return evidenceApi.getEvidenceViewer(evidenceLinkId, options);
    },
  };
}
