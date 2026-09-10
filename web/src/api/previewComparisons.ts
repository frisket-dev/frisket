import { httpContract } from './httpContract';
import { transcribeCompareWirePayload } from './transcribeComparePayload';
import { runEstimateFromV1Wire } from './actionEstimateValidation';
import type { ActionPreviewStartWire } from './actionPreviewRuns';
import type {
  OcrCompareScratchInput,
  RunEstimate,
  TopicSegmentationCompareScratchInput,
  TopicSegmentationCompareScratchResult,
  TranscribeCompareScratchInput,
} from './types';

type ContractErrorFactory = (status: number, payload: unknown) => Error;

function ocrPayload(input: OcrCompareScratchInput): OcrCompareScratchInput {
  return { ...input, ...(input.language !== undefined ? { language: input.language?.trim() || null } : {}) };
}

export interface PreviewComparisonsApi {
  compareOcrScratch(file: File, input: OcrCompareScratchInput): Promise<ActionPreviewStartWire>;
  estimateOcrScratch(file: File, input: OcrCompareScratchInput): Promise<RunEstimate>;
  compareTranscribeScratch(
    file: File,
    input: TranscribeCompareScratchInput,
  ): Promise<ActionPreviewStartWire>;
  estimateTranscribeScratch(file: File, input: TranscribeCompareScratchInput): Promise<RunEstimate>;
  compareTopicSegmentationScratch(
    file: File,
    input: TopicSegmentationCompareScratchInput,
  ): Promise<TopicSegmentationCompareScratchResult>;
}

export function createPreviewComparisonsApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): PreviewComparisonsApi {
  function upload(file: File, payload: object): FormData {
    const body = new FormData();
    body.append('file', file, file.name);
    body.append('payload', JSON.stringify(payload));
    return body;
  }
  return {
    compareOcrScratch(file, input) {
      return httpContract(
        'tenant.ocr_compare_scratch.post',
        { pathParams: { pid: projectId }, query: {}, body: upload(file, ocrPayload(input)), errorFactory },
      );
    },

    async estimateOcrScratch(file, input) {
      const params = { ...input };
      delete params.confirmation;
      const response = await httpContract('tenant.ocr_compare_scratch_estimate.post', {
        pathParams: { pid: projectId }, query: {}, body: upload(file, ocrPayload(params)), errorFactory,
      });
      return runEstimateFromV1Wire(response.estimate);
    },

    compareTranscribeScratch(file, input) {
      return httpContract(
        'tenant.transcribe_compare_scratch.post',
        { pathParams: { pid: projectId }, query: {}, body: upload(file, transcribeCompareWirePayload(input)), errorFactory },
      );
    },

    async estimateTranscribeScratch(file, input) {
      const params = { ...input };
      delete params.confirmation;
      const response = await httpContract('tenant.transcribe_compare_scratch_estimate.post', {
        pathParams: { pid: projectId }, query: {}, body: upload(file, transcribeCompareWirePayload(params)), errorFactory,
      });
      return runEstimateFromV1Wire(response.estimate);
    },

    compareTopicSegmentationScratch(file, input) {
      const body = new FormData();
      body.append('file', file, file.name);
      body.append('payload', JSON.stringify({
        variants: input.variants,
        language: input.language?.trim() || null,
      }));
      return httpContract(
        'tenant.topic_segmentation_compare_scratch.post',
        { pathParams: { pid: projectId }, query: {}, body, errorFactory },
      );
    },
  };
}
