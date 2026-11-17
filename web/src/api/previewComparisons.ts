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

function ocrPayload(input: OcrCompareScratchInput): OcrCompareScratchInput {
  return { ...input, ...(input.language !== undefined ? { language: input.language?.trim() || null } : {}) };
}

export function createPreviewComparisonsApi(projectId: string) {
  function upload(file: File, payload: object): FormData {
    const body = new FormData();
    body.append('file', file, file.name);
    body.append('payload', JSON.stringify(payload));
    return body;
  }
  return {
    compareOcrScratch(file: File, input: OcrCompareScratchInput): Promise<ActionPreviewStartWire> {
      return httpContract(
        'tenant.ocr_compare_scratch.post',
        { pathParams: { pid: projectId }, body: upload(file, ocrPayload(input)) },
      );
    },

    async estimateOcrScratch(file: File, input: OcrCompareScratchInput): Promise<RunEstimate> {
      const params = { ...input };
      delete params.confirmation;
      const response = await httpContract('tenant.ocr_compare_scratch_estimate.post', {
        pathParams: { pid: projectId }, body: upload(file, ocrPayload(params)),
      });
      return runEstimateFromV1Wire(response.estimate);
    },

    compareTranscribeScratch(
      file: File,
      input: TranscribeCompareScratchInput,
    ): Promise<ActionPreviewStartWire> {
      return httpContract(
        'tenant.transcribe_compare_scratch.post',
        { pathParams: { pid: projectId }, body: upload(file, transcribeCompareWirePayload(input)) },
      );
    },

    async estimateTranscribeScratch(
      file: File,
      input: TranscribeCompareScratchInput,
    ): Promise<RunEstimate> {
      const params = { ...input };
      delete params.confirmation;
      const response = await httpContract('tenant.transcribe_compare_scratch_estimate.post', {
        pathParams: { pid: projectId }, body: upload(file, transcribeCompareWirePayload(params)),
      });
      return runEstimateFromV1Wire(response.estimate);
    },

    compareTopicSegmentationScratch(
      file: File,
      input: TopicSegmentationCompareScratchInput,
    ): Promise<TopicSegmentationCompareScratchResult> {
      const body = new FormData();
      body.append('file', file, file.name);
      body.append('payload', JSON.stringify({
        variants: input.variants,
        language: input.language?.trim() || null,
      }));
      return httpContract(
        'tenant.topic_segmentation_compare_scratch.post',
        { pathParams: { pid: projectId }, body },
      );
    },
  };
}
