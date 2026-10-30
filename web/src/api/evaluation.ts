import type { ActionComparisonSource } from './actionComparison';
import type { JsonValue } from './types';
import { httpContract } from './httpContract';

export type EvaluationRule = 'exact' | 'ignore_case' | 'number' | 'date' | 'set' | 'hand';
export type EvaluationVerdict = 'match' | 'miss' | 'unchecked' | 'unscored' | 'not_run';
export interface EvaluationOutput { logical_key: string; column_id: number | null; current_name: string }
export interface EvaluationField {
  expected_column_id: number; output_column_id: number; logical_key: string;
  rule: EvaluationRule; hidden: boolean; source: 'import' | 'disagreements';
}
export interface EvaluationLinkInput {
  target_sheet_id: number; expected_sheet_id: number; action_id: string;
  keys: { expected_column_id: number; target_column_id: number | null }[];
  fields: EvaluationField[]; hidden: boolean;
}
export interface EvaluationLink extends EvaluationLinkInput {
  id: number; expected_sheet_name: string; target_sheet_name: string;
}
export type EvaluationVariantRef = { kind: 'current' }
  | { kind: 'preview'; preview_ids: string[] } | { kind: 'saved'; saved_id: number };
export interface EvaluationRequest {
  basis: ActionComparisonSource; row_ids: number[];
  variants: { key: string; label: string; source: EvaluationVariantRef }[];
  link_id: number | null;
}
export interface EvaluationCell {
  state: 'value' | 'null' | 'missing' | 'error' | 'pending' | 'cancelled';
  value?: JsonValue; error?: string | null;
}
export interface EvaluationAggregate {
  checked: number; attempted: number; matches: number; misses: number; failed: number;
  scored: number; remaining: number; accuracy: number | null; coverage: number;
}
export interface EvaluationVariant {
  key: string; label: string; source: EvaluationVariantRef; settings: JsonValue;
  rows: Record<string, Record<string, EvaluationCell>>;
  source_fingerprints: Record<string, string>;
  verdicts: Record<string, Record<string, EvaluationVerdict>>;
  scores: Record<string, EvaluationAggregate>;
}
export interface EvaluationResponse {
  sheet_id: number; action_id: string; output_columns: EvaluationOutput[]; row_ids: number[];
  link: EvaluationLink | null; expected: Record<string, Record<string, JsonValue>>;
  variants: EvaluationVariant[]; best: Record<string, string[]>;
  rule_previews: Record<string, Record<EvaluationRule, Record<string, EvaluationAggregate>>>;
}
export interface EvaluationJoinPreview {
  matched: number; not_found: number; ambiguous_expected: number; ambiguous_target: number;
  unmatched_expected_row_ids: number[]; ambiguous_expected_row_ids: number[]; ambiguous_target_row_ids: number[];
}
export interface ExpectedProjection {
  links: EvaluationLink[];
  columns: { id: string; link_id: number; expected_column_id: number; output_column_id: number;
    logical_key: string; name: string; rule: EvaluationRule; hidden: boolean; source: 'import' | 'disagreements' }[];
  rows: Record<string, Record<string, {
    value: JsonValue; source_row_id: number | null; source_column_id: number; checked: boolean;
  }>>;
}
export interface SavedSummary {
  id: number; label: string; sheet_id: number; action_id: string;
  output_columns: EvaluationOutput[]; row_count: number; saved_at: string;
}
export interface SavedItem extends SavedSummary {
  settings: JsonValue; rows: Record<string, Record<string, EvaluationCell>>;
  source_fingerprints: Record<string, string>;
}
export interface EvaluationJudgment {
  comparison: EvaluationRequest; variant_key: string; row_id: number; logical_key: string;
  verdict: 'match' | 'miss' | null;
}
export interface ExpectedAnswerWrite {
  basis: ActionComparisonSource; link_id: number | null; row_id: number; logical_key: string;
  value: JsonValue; rule: EvaluationRule;
}
export interface EvaluationApi {
  getLinks(sheetId: number): Promise<{ links: EvaluationLink[] }>;
  previewLink(input: EvaluationLinkInput): Promise<EvaluationJoinPreview>;
  saveLink(input: EvaluationLinkInput, id?: number): Promise<EvaluationLink>;
  deleteLink(id: number): Promise<void>;
  projectExpected(sheetId: number, rowIds: number[]): Promise<ExpectedProjection>;
  score(request: EvaluationRequest): Promise<EvaluationResponse>;
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
    getLinks: sheetId => httpContract('tenant.evaluation_links.get', { ...base, query: { sheet_id: sheetId } }),
    previewLink: body => httpContract('tenant.evaluation_link_preview.post', { ...base, body }),
    saveLink: (body, id) => id === undefined
      ? httpContract('tenant.evaluation_link_create.post', { ...base, body })
      : httpContract('tenant.evaluation_link_update.put', { ...base, pathParams: { pid: projectId, link_id: id }, body }),
    deleteLink: async id => { await httpContract('tenant.evaluation_link_delete.delete', {
      ...base, pathParams: { pid: projectId, link_id: id },
    }); },
    projectExpected: (sheetId, rowIds) => httpContract('tenant.evaluation_projection.post', {
      ...base, body: { sheet_id: sheetId, row_ids: rowIds },
    }),
    score: body => httpContract('tenant.evaluation_score.post', { ...base, body }),
    setRule: (columnId, rule) => httpContract('tenant.evaluation_rule.put', {
      ...base, pathParams: { pid: projectId, expected_column_id: columnId }, body: { rule },
    }),
    setJudgment: body => httpContract('tenant.evaluation_judgment.put', { ...base, body }),
    saveExpectedAnswer: body => httpContract('tenant.evaluation_expected_answer.post', { ...base, body }),
    getShelf: (sheetId, actionId) => httpContract('tenant.evaluation_shelf.get', {
      ...base, query: { sheet_id: sheetId, action_id: actionId },
    }),
    saveShelf: body => httpContract('tenant.evaluation_shelf_save.post', { ...base, body }),
    getShelfItem: id => httpContract('tenant.evaluation_shelf_item.get', { ...base, pathParams: { pid: projectId, saved_id: id } }),
    renameShelfItem: (id, label) => httpContract('tenant.evaluation_shelf_rename.patch', {
      ...base, pathParams: { pid: projectId, saved_id: id }, body: { label },
    }),
    deleteShelfItem: async id => { await httpContract('tenant.evaluation_shelf_delete.delete', {
      ...base, pathParams: { pid: projectId, saved_id: id },
    }); },
    downloadCsv: body => httpContract('tenant.evaluation_download_csv.post', { ...base, body }),
    downloadSettings: body => httpContract('tenant.evaluation_download_settings.post', { ...base, body }),
  };
}
