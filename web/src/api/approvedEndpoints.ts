import type { HttpApprovedEndpoints } from '../generated/openHttpContracts';
import { httpContract, type RequestOptions } from './httpContract';

export type ApprovedEndpoint = NonNullable<HttpApprovedEndpoints['endpoints']>[number];

/** Local keeps the list for the workspace; Team keeps it for the organization,
 *  readable by members and replaceable by organization owners. */
export type ApprovedEndpointsScope = 'workspace' | 'organization';

function endpointsOf(wire: HttpApprovedEndpoints): ApprovedEndpoint[] {
  return wire.endpoints ?? [];
}

export function listApprovedEndpoints(
  scope: ApprovedEndpointsScope,
  options: RequestOptions = {},
): Promise<ApprovedEndpoint[]> {
  return scope === 'organization'
    ? httpContract('outer.list_organization_approved_endpoints.get', { signal: options.signal }, endpointsOf)
    : httpContract('tenant.list_approved_endpoints.get', { signal: options.signal }, endpointsOf);
}

export function replaceApprovedEndpoints(
  scope: ApprovedEndpointsScope,
  endpoints: ApprovedEndpoint[],
  options: RequestOptions = {},
): Promise<ApprovedEndpoint[]> {
  const body = { endpoints };
  return scope === 'organization'
    ? httpContract('outer.replace_organization_approved_endpoints.put', { body, signal: options.signal }, endpointsOf)
    : httpContract('tenant.replace_approved_endpoints.put', { body, signal: options.signal }, endpointsOf);
}
