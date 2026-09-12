import { httpContract } from './httpContract';
import type {
  HttpModelsGatewayCandidateRequest,
  HttpModelsGatewaySaveRequest,
  HttpModelsGatewayStatus,
  HttpModelsGatewayValidationResponse,
} from '../generated/openHttpContracts';

export type ModelsGatewayScope = 'workspace' | 'organization';

export function getModelsGateway(scope: ModelsGatewayScope, signal?: AbortSignal): Promise<HttpModelsGatewayStatus> {
  const options = { pathParams: {}, query: {}, signal };
  return scope === 'organization'
    ? httpContract('outer.get_org_models_gateway.get', options)
    : httpContract('tenant.get_models_gateway.get', options);
}

export function validateModelsGateway(
  scope: ModelsGatewayScope,
  body: HttpModelsGatewayCandidateRequest,
  signal?: AbortSignal,
): Promise<HttpModelsGatewayValidationResponse> {
  const options = { pathParams: {}, query: {}, body, signal };
  return scope === 'organization'
    ? httpContract('outer.validate_org_models_gateway.post', options)
    : httpContract('tenant.validate_models_gateway.post', options);
}

export function saveModelsGateway(
  scope: ModelsGatewayScope,
  body: HttpModelsGatewaySaveRequest,
  signal?: AbortSignal,
): Promise<HttpModelsGatewayStatus> {
  const options = { pathParams: {}, query: {}, body, signal };
  return scope === 'organization'
    ? httpContract('outer.set_org_models_gateway.put', options)
    : httpContract('tenant.set_models_gateway.put', options);
}
