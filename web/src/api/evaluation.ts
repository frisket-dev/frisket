import { sourceForWire, type ActionComparisonSource } from './actionComparison';
import type {
  HttpComparisonCaptureRequest, HttpEvaluationCell, HttpEvaluationCellRequest,
  HttpEvaluationJudgmentRequest, HttpEvaluationRequest, HttpEvaluationResponse,
  HttpExpectedAnswerRequest, HttpExpectedJoinPreview, HttpExpectedLink, HttpExpectedLinkInput,
  HttpExpectedProjection, HttpExpectedRuleSetting, HttpSavedComparison, HttpSavedComparisonSummary,
} from '../generated/openHttpContracts';
import { httpContract } from './httpContract';
import { downloadEvaluation } from './raw/evaluationDownloads';

// API wire shapes come from the server schema. The UI requires field defaults
// that Pydantic always supplies in responses, so those are normalized below.
export type EvaluationRule = HttpExpectedRuleSetting['rule'];
export type EvaluationVerdict = HttpEvaluationResponse['variants'][number]['verdicts'][string][string];
export type EvaluationOutput = HttpEvaluationResponse['output_columns'][number];
export type EvaluationField = Required<HttpExpectedLink['fields'][number]>;
export type EvaluationLinkInput = Omit<HttpExpectedLinkInput, 'fields' | 'hidden'> & {
  fields: EvaluationField[]; hidden: boolean;
};
export type EvaluationLink = Omit<HttpExpectedLink, 'fields' | 'hidden'> & {
  fields: EvaluationField[]; hidden: boolean;
};
export type EvaluationVariantRef = HttpEvaluationRequest['variants'][number]['source'];
export type EvaluationRequest = Omit<HttpEvaluationRequest, 'basis' | 'link_id'> & {
  basis: ActionComparisonSource; link_id: number | null;
};
export type EvaluationBoundedValue = Required<NonNullable<HttpEvaluationCell['bounded_value']>>;
export type EvaluationCell = Omit<HttpEvaluationCell, 'bounded_value'> & { bounded_value?: EvaluationBoundedValue | null };
export type EvaluationCellRequest = Omit<HttpEvaluationCellRequest, 'comparison'> & { comparison: EvaluationRequest };
export type EvaluationCaptureRequest = Omit<HttpComparisonCaptureRequest, 'comparison'> & { comparison: EvaluationRequest };
export type EvaluationAggregate = HttpEvaluationResponse['variants'][number]['scores'][string];
export type EvaluationVariant = Omit<HttpEvaluationResponse['variants'][number], 'rows'> & {
  rows: Record<string, Record<string, EvaluationCell>>;
};
export type EvaluationResponse = Omit<HttpEvaluationResponse, 'link' | 'variants' | 'expected_bounded'> & {
  link: EvaluationLink | null; variants: EvaluationVariant[];
  expected_bounded?: Record<string, Record<string, EvaluationBoundedValue>>;
};
export type EvaluationJoinPreview = HttpExpectedJoinPreview;
export type ExpectedProjection = Omit<HttpExpectedProjection, 'links'> & { links: EvaluationLink[] };
export type SavedSummary = HttpSavedComparisonSummary;
export type SavedItem = Omit<HttpSavedComparison, 'rows'> & {
  rows: Record<string, Record<string, EvaluationCell>>;
};
export type EvaluationJudgment = Omit<HttpEvaluationJudgmentRequest, 'comparison'> & { comparison: EvaluationRequest };
export type ExpectedAnswerWrite = Omit<HttpExpectedAnswerRequest, 'basis' | 'link_id' | 'rule'> & {
  basis: ActionComparisonSource; link_id: number | null; rule: EvaluationRule;
};

function normalizeLink(link: HttpExpectedLink): EvaluationLink {
  return { ...link, hidden: link.hidden ?? false, fields: link.fields.map(field => ({
    ...field, rule: field.rule ?? 'exact', hidden: field.hidden ?? false, source: field.source ?? 'import',
  })) };
}

function wireRequest(request: EvaluationRequest): HttpEvaluationRequest {
  return { ...request, basis: sourceForWire(request.basis) };
}

function normalizeBounded(value: NonNullable<HttpEvaluationCell['bounded_value']>): EvaluationBoundedValue {
  return { ...value, truncated: true };
}

function normalizeCell(cell: HttpEvaluationCell): EvaluationCell {
  const { bounded_value, ...rest } = cell;
  return { ...rest, ...(bounded_value === undefined ? {} : { bounded_value: bounded_value ? normalizeBounded(bounded_value) : null }) };
}

function normalizeRows(rows: Record<string, Record<string, HttpEvaluationCell>>): Record<string, Record<string, EvaluationCell>> {
  return Object.fromEntries(Object.entries(rows).map(([row, cells]) => [row,
    Object.fromEntries(Object.entries(cells).map(([key, cell]) => [key, normalizeCell(cell)]))]));
}

function normalizeResponse(response: HttpEvaluationResponse): EvaluationResponse {
  const { expected_bounded, ...rest } = response;
  return { ...rest, link: response.link ? normalizeLink(response.link) : null,
    variants: response.variants.map(variant => ({ ...variant, rows: normalizeRows(variant.rows) })),
    ...(expected_bounded ? { expected_bounded: Object.fromEntries(Object.entries(expected_bounded).map(
      ([row, cells]) => [row, Object.fromEntries(Object.entries(cells).map(([key, value]) => [key, normalizeBounded(value)]))],
    )) } : {}) };
}

export interface EvaluationApi {
  getLinks(sheetId: number): Promise<{ links: EvaluationLink[] }>;
  previewLink(input: EvaluationLinkInput): Promise<EvaluationJoinPreview>;
  saveLink(input: EvaluationLinkInput, id?: number): Promise<EvaluationLink>;
  deleteLink(id: number): Promise<void>;
  projectExpected(sheetId: number, rowIds: number[]): Promise<ExpectedProjection>;
  score(request: EvaluationRequest): Promise<EvaluationResponse>;
  getCell(request: EvaluationCellRequest): Promise<EvaluationCell>;
  captureComparison(request: EvaluationCaptureRequest): Promise<{ items: SavedSummary[] }>;
  discardCapture(id: number): Promise<void>;
  setRule(columnId: number, rule: EvaluationRule): Promise<{ expected_column_id: number; rule: EvaluationRule }>;
  setJudgment(input: EvaluationJudgment): Promise<EvaluationResponse>;
  saveExpectedAnswer(input: ExpectedAnswerWrite): Promise<EvaluationLink>;
  getShelf(sheetId: number, actionId: string): Promise<{ items: SavedSummary[] }>;
  saveShelf(input: { comparison: EvaluationRequest; variant_keys: string[] }): Promise<{ items: SavedSummary[] }>;
  getShelfItem(id: number): Promise<SavedItem>;
  renameShelfItem(id: number, label: string): Promise<SavedSummary>;
  deleteShelfItem(id: number): Promise<void>;
  downloadCsv(request: EvaluationRequest): Promise<Blob>;
  downloadSettings(request: EvaluationRequest): Promise<Blob>;
}

export function createEvaluationApi(
  errorFactory: (status: number, payload: unknown) => Error,
  projectId: string,
): EvaluationApi {
  const base = { pathParams: { pid: projectId }, query: {}, errorFactory };
  return {
    getLinks: sheetId => httpContract('tenant.evaluation_links.get', { ...base, query: { sheet_id: sheetId } }, wire => ({ links: wire.links.map(normalizeLink) })),
    previewLink: body => httpContract('tenant.evaluation_link_preview.post', { ...base, body }),
    saveLink: (body, id) => id === undefined
      ? httpContract('tenant.evaluation_link_create.post', { ...base, body }, normalizeLink)
      : httpContract('tenant.evaluation_link_update.put', { ...base, pathParams: { pid: projectId, link_id: id }, body }, normalizeLink),
    deleteLink: async id => { await httpContract('tenant.evaluation_link_delete.delete', {
      ...base, pathParams: { pid: projectId, link_id: id },
    }); },
    projectExpected: (sheetId, rowIds) => httpContract('tenant.evaluation_projection.post', {
      ...base, body: { sheet_id: sheetId, row_ids: rowIds },
    }, wire => ({ ...wire, links: wire.links.map(normalizeLink) })),
    score: body => httpContract('tenant.evaluation_score.post', { ...base, body: wireRequest(body) }, normalizeResponse),
    getCell: body => httpContract('tenant.evaluation_cell.post', { ...base, body: { ...body, comparison: wireRequest(body.comparison) } }, normalizeCell),
    captureComparison: body => httpContract('tenant.evaluation_capture.post', { ...base, body: { ...body, comparison: wireRequest(body.comparison) } }),
    discardCapture: async id => { await httpContract('tenant.evaluation_capture_discard.delete', {
      ...base, pathParams: { pid: projectId, saved_id: id },
    }); },
    setRule: (columnId, rule) => httpContract('tenant.evaluation_rule_update.put', {
      ...base, pathParams: { pid: projectId, expected_column_id: columnId }, body: { rule },
    }),
    setJudgment: body => httpContract('tenant.evaluation_judgment.put', { ...base, body: { ...body, comparison: wireRequest(body.comparison) } }, normalizeResponse),
    saveExpectedAnswer: body => httpContract('tenant.evaluation_expected_answer.post', { ...base, body: { ...body, basis: sourceForWire(body.basis) } }, normalizeLink),
    getShelf: (sheetId, actionId) => httpContract('tenant.evaluation_shelf_list.get', {
      ...base, query: { sheet_id: sheetId, action_id: actionId },
    }),
    saveShelf: body => httpContract('tenant.evaluation_shelf_save.post', { ...base, body: { ...body, comparison: wireRequest(body.comparison) } }),
    getShelfItem: id => httpContract('tenant.evaluation_shelf_get.get', { ...base, pathParams: { pid: projectId, saved_id: id } }, wire => ({ ...wire, rows: normalizeRows(wire.rows) })),
    renameShelfItem: (id, label) => httpContract('tenant.evaluation_shelf_rename.patch', {
      ...base, pathParams: { pid: projectId, saved_id: id }, body: { label },
    }),
    deleteShelfItem: async id => { await httpContract('tenant.evaluation_shelf_delete.delete', {
      ...base, pathParams: { pid: projectId, saved_id: id },
    }); },
    downloadCsv: body => downloadEvaluation(projectId, 'download.csv', body, errorFactory),
    downloadSettings: body => downloadEvaluation(projectId, 'settings.json', body, errorFactory),
  };
}
