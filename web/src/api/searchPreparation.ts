import type { HttpContractOperationMap } from '../generated/openHttpContracts';
import { httpContract, type HttpContractSuccessResponse } from './httpContract';

type SearchPreparationWire =
  HttpContractSuccessResponse<'tenant.get_search_preparation.get'>;
type ListedSearchPreparationWire =
  HttpContractSuccessResponse<'tenant.list_search_preparations.get'>['batches'][number];
type SearchPreparationTextSamplesWire =
  HttpContractSuccessResponse<'tenant.get_search_preparation_text_samples.get'>;

export type SearchPreparationBatch = SearchPreparationWire | ListedSearchPreparationWire;
export type SearchPreparationStatus = SearchPreparationBatch['status'];
export type SearchPreparationGroup = SearchPreparationBatch['groups'][number];
export type SearchPreparationModel = SearchPreparationBatch['models'][number];
export type SearchPreparationJob = SearchPreparationBatch['jobs'][number];
export type SearchPreparationTextSample = SearchPreparationTextSamplesWire['samples'][number];

export interface SearchPreparationRequestOptions {
  signal?: AbortSignal;
}

export type CreateSearchPreparationInput =
  HttpContractOperationMap['tenant.create_search_preparation.post']['request'];
export type DecideSearchPreparationInput =
  HttpContractOperationMap['tenant.decide_search_preparation.post']['request'];

export function createSearchPreparation(
  projectId: string,
  input: CreateSearchPreparationInput,
  options: SearchPreparationRequestOptions = {},
): Promise<SearchPreparationWire> {
  return httpContract('tenant.create_search_preparation.post', {
    pathParams: { pid: projectId },
    query: {},
    body: input,
    signal: options.signal,
  });
}

export function listSearchPreparations(
  projectId: string,
  options: SearchPreparationRequestOptions = {},
): Promise<HttpContractSuccessResponse<'tenant.list_search_preparations.get'>> {
  return httpContract('tenant.list_search_preparations.get', {
    pathParams: { pid: projectId },
    query: {},
    signal: options.signal,
  });
}

export function getSearchPreparation(
  projectId: string,
  batchId: string,
  options: SearchPreparationRequestOptions = {},
): Promise<SearchPreparationWire> {
  return httpContract('tenant.get_search_preparation.get', {
    pathParams: { pid: projectId, batch_id: batchId },
    query: {},
    signal: options.signal,
  });
}

export function decideSearchPreparation(
  projectId: string,
  batchId: string,
  input: DecideSearchPreparationInput,
  options: SearchPreparationRequestOptions = {},
): Promise<SearchPreparationWire> {
  return httpContract('tenant.decide_search_preparation.post', {
    pathParams: { pid: projectId, batch_id: batchId },
    query: {},
    body: input,
    signal: options.signal,
  });
}

export function stopSearchPreparation(
  projectId: string,
  batchId: string,
  options: SearchPreparationRequestOptions = {},
): Promise<SearchPreparationWire> {
  return httpContract('tenant.stop_search_preparation.post', {
    pathParams: { pid: projectId, batch_id: batchId },
    query: {},
    signal: options.signal,
  });
}

export function getSearchPreparationTextSamples(
  projectId: string,
  batchId: string,
  selection: { rowId?: number; pageNumber?: number } = {},
  options: SearchPreparationRequestOptions = {},
): Promise<SearchPreparationTextSamplesWire> {
  return httpContract('tenant.get_search_preparation_text_samples.get', {
    pathParams: { pid: projectId, batch_id: batchId },
    query: {
      row_id: selection.rowId,
      page_number: selection.pageNumber,
    },
    signal: options.signal,
  });
}
