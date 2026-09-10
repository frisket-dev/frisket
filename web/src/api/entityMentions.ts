import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import type {
  EntityMentionDocumentsPage,
  EntityMentionGroup,
  EntityMentionOccurrencesPage,
  EntityMentionSelector,
  EntityMentionSurface,
  EntityMentionTypeTotal,
  EntityMentionsPreview,
} from './types';

export interface EntityMentionsOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface EntityMentionsPreviewInput {
  sheetId: string;
  columnId: string;
  search?: string;
  type?: string;
  limit?: number;
  offset?: number;
}

export interface EntityMentionDocumentsInput {
  sheetId: string;
  columnId: string;
  type: string;
  fingerprint?: string;
  text?: string;
  limit?: number;
  offset?: number;
}

export interface EntityMentionOccurrencesInput {
  sheetId: string;
  rowId: string;
  columnId: string;
  type: string;
  fingerprint?: string;
  text?: string;
  limit?: number;
  offset?: number;
  snippetRadius?: number;
}

type EntityMentionsPreviewWire =
  HttpContractSuccessResponse<'tenant.entity_mentions_preview.post'>;
type EntityMentionDocumentsWire =
  HttpContractSuccessResponse<'tenant.entity_mention_documents.post'>;
type EntityMentionOccurrencesWire =
  HttpContractSuccessResponse<'tenant.entity_mention_occurrences.post'>;
type EntityMentionSelectorWire =
  | EntityMentionsPreviewWire['items'][number]['selector']
  | EntityMentionDocumentsWire['selector']
  | EntityMentionOccurrencesWire['selector'];

export interface EntityMentionsApi {
  preview(
    projectId: string,
    input: EntityMentionsPreviewInput,
    options?: EntityMentionsOptions,
  ): Promise<EntityMentionsPreview>;
  documents(
    projectId: string,
    input: EntityMentionDocumentsInput,
    options?: EntityMentionsOptions,
  ): Promise<EntityMentionDocumentsPage>;
  occurrences(
    projectId: string,
    input: EntityMentionOccurrencesInput,
    options?: EntityMentionsOptions,
  ): Promise<EntityMentionOccurrencesPage>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;

function entityMentionSelector(
  selector: EntityMentionSelectorWire,
): EntityMentionSelector {
  if (selector.kind === 'fingerprint') {
    return { kind: 'fingerprint', fingerprint: selector.fingerprint };
  }
  return { kind: 'text', text: selector.text };
}

function mapDocuments(
  wire: EntityMentionDocumentsWire,
): EntityMentionDocumentsPage {
  return {
    sheetId: wire.sheet_id.toString(),
    columnId: wire.column.id.toString(),
    type: wire.type,
    selector: entityMentionSelector(wire.selector),
    totals: {
      mentions: wire.totals.mentions,
      documents: wire.totals.documents,
    },
    documents: wire.documents.map((document) => ({
      rowId: document.row_id.toString(),
      title: document.title,
      occurrenceCount: document.occurrence_count,
    })),
    nextOffset: wire.next_offset,
  };
}

function mapOccurrences(
  wire: EntityMentionOccurrencesWire,
): EntityMentionOccurrencesPage {
  return {
    sheetId: wire.sheet_id.toString(),
    rowId: wire.row_id.toString(),
    columnId: wire.column.id.toString(),
    type: wire.type,
    selector: entityMentionSelector(wire.selector),
    textColumn: wire.text_column === null
      ? null
      : {
          id: wire.text_column.id.toString(),
          name: wire.text_column.name,
        },
    totals: { occurrences: wire.totals.occurrences },
    occurrences: wire.occurrences.map((occurrence) => ({
      occurrenceId: occurrence.occurrence_id,
      start: occurrence.start,
      end: occurrence.end,
      quote: occurrence.quote,
      snippet: {
        text: occurrence.snippet.text,
        markStart: occurrence.snippet.mark_start,
        markEnd: occurrence.snippet.mark_end,
        truncatedStart: occurrence.snippet.truncated_start,
        truncatedEnd: occurrence.snippet.truncated_end,
      },
    })),
    nextOffset: wire.next_offset,
    unpositioned: wire.unpositioned === null
      ? null
      : { reason: wire.unpositioned.reason, total: wire.unpositioned.total },
  };
}

function mapPreview(
  wire: EntityMentionsPreviewWire,
): EntityMentionsPreview {
  return {
    sheetId: wire.sheet_id.toString(),
    column: {
      id: wire.column.id.toString(),
      name: wire.column.name,
      semanticType: wire.column.semantic_type,
    },
    coverage: {
      targetRows: wire.coverage.target_rows,
      completedRows: wire.coverage.completed_rows,
      failedRows: wire.coverage.failed_rows,
      sheetRows: wire.coverage.sheet_rows,
      scopeKind: wire.coverage.scope_kind,
    },
    search: wire.search,
    type: wire.type,
    totalGroups: wire.total_groups,
    typeTotals: wire.type_totals.map((total): EntityMentionTypeTotal => ({
      type: total.type,
      totalGroups: total.total_groups,
    })),
    limit: wire.limit,
    offset: wire.offset,
    items: wire.items.map((item): EntityMentionGroup => ({
      type: item.type,
      selector: entityMentionSelector(item.selector),
      label: item.label,
      rowCount: item.row_count,
      mentionCount: item.mention_count,
      surfaceCount: item.surface_count,
      surfaces: item.surfaces.map((surface): EntityMentionSurface => ({
        text: surface.text,
        rowCount: surface.row_count,
        mentionCount: surface.mention_count,
      })),
    })),
  };
}

export function createEntityMentionsApi(
  errorFactory: ContractErrorFactory,
): EntityMentionsApi {
  return {
    preview(projectId, input, options = {}) {
      return httpContract(
        'tenant.entity_mentions_preview.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: {
            sheet_id: Number(input.sheetId),
            column_id: Number(input.columnId),
            ...(input.search?.trim() ? { search: input.search } : {}),
            ...(input.type?.trim() ? { type: input.type } : {}),
            ...(input.limit != null ? { limit: input.limit } : {}),
            ...(input.offset != null ? { offset: input.offset } : {}),
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        mapPreview,
      );
    },

    documents(projectId, input, options = {}) {
      return httpContract(
        'tenant.entity_mention_documents.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: {
            sheet_id: Number(input.sheetId),
            column_id: Number(input.columnId),
            type: input.type,
            ...(input.fingerprint !== undefined ? { fingerprint: input.fingerprint } : {}),
            ...(input.text !== undefined ? { text: input.text } : {}),
            ...(input.limit != null ? { limit: input.limit } : {}),
            ...(input.offset != null ? { offset: input.offset } : {}),
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        mapDocuments,
      );
    },

    occurrences(projectId, input, options = {}) {
      return httpContract(
        'tenant.entity_mention_occurrences.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: {
            sheet_id: Number(input.sheetId),
            row_id: Number(input.rowId),
            column_id: Number(input.columnId),
            type: input.type,
            ...(input.fingerprint !== undefined ? { fingerprint: input.fingerprint } : {}),
            ...(input.text !== undefined ? { text: input.text } : {}),
            ...(input.limit != null ? { limit: input.limit } : {}),
            ...(input.offset != null ? { offset: input.offset } : {}),
            ...(input.snippetRadius != null ? { snippet_radius: input.snippetRadius } : {}),
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        mapOccurrences,
      );
    },
  };
}
