import type { HttpGuideSampleLaunchResponse, HttpGuideSampleMetadata } from '../generated/openHttpContracts';
import { httpContract } from './httpContract';

export type GuideSampleMetadata = HttpGuideSampleMetadata;

/** Create the project through the normal ownership path before preparing its sample. */
export const guideSamplesApi = {
  get(sampleId: string, revision: number, signal?: AbortSignal): Promise<GuideSampleMetadata> {
    return httpContract('tenant.get_guide_sample.get', {
      pathParams: { sample_id: sampleId }, query: { revision }, signal,
    }, (wire) => wire);
  },
  async launch(sampleId: string, revision: number, operationId: string, title: string): Promise<HttpGuideSampleLaunchResponse> {
    const project = await httpContract('tenant.create_project.post', {
      pathParams: {}, query: {}, body: { name: title }, headers: { 'Idempotency-Key': operationId },
    }, (wire) => wire);
    return httpContract('tenant.prepare_guide_sample.post', {
      pathParams: { pid: project.id, sample_id: sampleId }, query: {},
      body: { revision, operation_id: operationId },
    }, (wire) => wire);
  },
};
