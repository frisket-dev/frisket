import {
  HTTP_CONTRACT_ARTIFACT,
  type HttpContractOperationId,
  type HttpContractOperationMap,
} from '../generated/openHttpContracts';
import { errorFromContract } from './contractErrors';

type FetchImplementation = typeof globalThis.fetch;
type DecimalDigit = '0' | '1' | '2' | '3' | '4' | '5' | '6' | '7' | '8' | '9';
type SuccessStatus = `2${DecimalDigit}${DecimalDigit}`;
type Operation<Id extends HttpContractOperationId> = HttpContractOperationMap[Id];
type OperationResponses<Id extends HttpContractOperationId> = Operation<Id>['responses'];
type GeneratedEndpoint = (typeof HTTP_CONTRACT_ARTIFACT.endpoints)[number];

export type HttpContractSuccessResponse<Id extends HttpContractOperationId> =
  OperationResponses<Id>[Extract<keyof OperationResponses<Id>, SuccessStatus>];

type RequestBodyOptions<Id extends HttpContractOperationId> =
  Operation<Id>['request'] extends undefined
    ? { body?: never }
    : undefined extends Operation<Id>['request']
      ? { body?: Exclude<Operation<Id>['request'], undefined> }
      : { body: Operation<Id>['request'] };

// An operation without path or query parameters, or whose parameters are all
// optional, may omit the corresponding object.
type ParamOptions<Name extends 'pathParams' | 'query', Value> =
  Record<string, never> extends Value
    ? { [Key in Name]?: Value }
    : { [Key in Name]: Value };

/** Maps a non-2xx response to the error the call rejects with. */
export type ContractErrorFactory = (status: number, payload: unknown) => Error;

/** Per-call options a domain API method forwards to `httpContract`. */
export interface RequestOptions {
  signal?: AbortSignal;
}

export type HttpContractInvokeOptions<Id extends HttpContractOperationId> = {
  fetch?: FetchImplementation;
  signal?: AbortSignal;
  headers?: HeadersInit;
  credentials?: RequestCredentials;
  keepalive?: boolean;
  /** Overrides the shared contract error mapping for this call. */
  errorFactory?: ContractErrorFactory;
} & ParamOptions<'pathParams', Operation<Id>['pathParams']>
  & ParamOptions<'query', Operation<Id>['query']>
  & RequestBodyOptions<Id>;

function renderPath(template: string, pathParams: object): string {
  const params = new Map(Object.entries(pathParams));
  return template.replace(/\{([^}]+)\}/g, (_match, name: string) => {
    if (!params.has(name)) throw new Error(`missing rendered path parameter ${name}`);
    return encodeURIComponent(String(params.get(name)));
  });
}

function appendQuery(path: string, query: object): string {
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(query)) {
    if (value === undefined) continue;
    if (Array.isArray(value)) {
      for (const item of value) search.append(name, item === null ? 'null' : String(item));
    } else {
      search.append(name, value === null ? 'null' : String(value));
    }
  }
  const encoded = search.toString();
  return encoded ? `${path}?${encoded}` : path;
}

function omitUndefinedProperties<Value>(value: Value): Value {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return value;
  const prototype = Object.getPrototypeOf(value);
  if (
    (prototype !== Object.prototype && prototype !== null)
    || Object.getOwnPropertySymbols(value).length > 0
  ) return value;
  const entries = Object.entries(value);
  if (entries.every(([, child]) => child !== undefined)) return value;
  const cleaned = Object.create(prototype) as Record<string, unknown>;
  for (const [name, child] of entries) {
    if (child !== undefined) cleaned[name] = child;
  }
  return cleaned as Value;
}

function operationMetadata(endpointId: HttpContractOperationId): GeneratedEndpoint {
  const endpoint = HTTP_CONTRACT_ARTIFACT.endpoints.find(
    (candidate) => candidate.id === endpointId,
  );
  if (!endpoint) throw new Error(`Unknown generated HTTP contract ${endpointId}`);
  return endpoint;
}

function headersWithContentType(headers: HeadersInit | undefined, mediaType: string): HeadersInit {
  const inspected = new Headers(headers);
  if (inspected.has('Content-Type') && headers !== undefined) return headers;
  inspected.set('Content-Type', mediaType);
  return inspected;
}

export function httpContract<Id extends HttpContractOperationId>(
  endpointId: Id,
  options: HttpContractInvokeOptions<Id>,
): Promise<HttpContractSuccessResponse<Id>>;
export function httpContract<Id extends HttpContractOperationId, DomainResponse>(
  endpointId: Id,
  options: HttpContractInvokeOptions<Id>,
  mapResponse: (wire: HttpContractSuccessResponse<Id>) => DomainResponse,
): Promise<DomainResponse>;
export async function httpContract<Id extends HttpContractOperationId, DomainResponse>(
  endpointId: Id,
  options: HttpContractInvokeOptions<Id>,
  mapResponse?: (wire: HttpContractSuccessResponse<Id>) => DomainResponse,
): Promise<HttpContractSuccessResponse<Id> | DomainResponse> {
  const endpoint = operationMetadata(endpointId);
  const requestInit: RequestInit = { method: endpoint.method };
  if (options.signal !== undefined) requestInit.signal = options.signal;
  if (options.credentials !== undefined) requestInit.credentials = options.credentials;
  if (options.keepalive !== undefined) requestInit.keepalive = options.keepalive;

  if (endpoint.request !== null) {
    if (options.body === undefined) {
      if (endpoint.request.required) {
        const bodyKind = endpoint.request.mediaType === 'application/json'
          ? 'JSON'
          : endpoint.request.mediaType;
        throw new TypeError(`${endpointId} requires a ${bodyKind} request body`);
      }
      if (options.headers !== undefined) requestInit.headers = options.headers;
    } else if (endpoint.request.mediaType === 'multipart/form-data') {
      if (!(options.body instanceof FormData)) {
        throw new TypeError(`${endpointId} requires a native FormData request body`);
      }
      // Fetch owns the multipart boundary. Supplying Content-Type here would
      // discard that boundary and make the FastAPI upload unreadable.
      if (options.headers !== undefined) requestInit.headers = options.headers;
      requestInit.body = options.body;
    } else {
      requestInit.headers = headersWithContentType(options.headers, endpoint.request.mediaType);
      requestInit.body = JSON.stringify(omitUndefinedProperties(options.body));
    }
  } else {
    if (options.body !== undefined) {
      throw new TypeError(`${endpointId} does not declare a JSON request body`);
    }
    if (options.headers !== undefined) requestInit.headers = options.headers;
  }

  const requestPath = appendQuery(
    renderPath(endpoint.path, options.pathParams ?? {}),
    options.query ?? {},
  );
  const response = await (options.fetch ?? globalThis.fetch)(requestPath, requestInit);
  const isSuccess = response.status >= 200 && response.status < 300;

  if (
    isSuccess
    && ((endpoint.method as string) === 'HEAD' || response.status === 204 || response.status === 205)
  ) {
    const wire = undefined as HttpContractSuccessResponse<Id>;
    return mapResponse ? mapResponse(wire) : wire;
  }

  if (!isSuccess) {
    let payload: unknown;
    try {
      payload = await response.json();
    } catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
      payload = {
        detail: response.statusText || `Request failed (HTTP ${response.status})`,
      };
    }
    throw (options.errorFactory ?? errorFromContract)(response.status, payload);
  }
  const payload: unknown = await response.json();
  const wire = payload as HttpContractSuccessResponse<Id>;
  return mapResponse ? mapResponse(wire) : wire;
}
