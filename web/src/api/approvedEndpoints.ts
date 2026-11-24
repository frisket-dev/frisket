import type { HttpApprovedEndpointList, HttpApprovedEndpoints } from '../generated/openHttpContracts';
import { httpContract, type RequestOptions } from './httpContract';

export type ApprovedEndpoint = NonNullable<HttpApprovedEndpoints['endpoints']>[number];

/** Local keeps the list for the workspace; Team keeps it for the organization,
 *  readable by members and replaceable by organization owners. */
export type ApprovedEndpointsScope = 'workspace' | 'organization';

/** The list as this reader sees it, and whether this reader may change it. */
export interface ApprovedEndpointList {
  endpoints: ApprovedEndpoint[];
  editable: boolean;
}

function endpointsOf(wire: HttpApprovedEndpoints): ApprovedEndpoint[] {
  return wire.endpoints ?? [];
}

function listOf(wire: HttpApprovedEndpointList): ApprovedEndpointList {
  return { endpoints: wire.endpoints ?? [], editable: wire.editable };
}

export function listApprovedEndpoints(
  scope: ApprovedEndpointsScope,
  options: RequestOptions = {},
): Promise<ApprovedEndpointList> {
  return scope === 'organization'
    ? httpContract('outer.list_organization_approved_endpoints.get', { signal: options.signal }, listOf)
    : httpContract('tenant.list_approved_endpoints.get', { signal: options.signal }, listOf);
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
