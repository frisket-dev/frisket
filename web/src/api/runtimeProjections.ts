import type {
  HttpRuntimeProjectionArtifactRequest,
  Http_RuntimeProjectionBuildRequest,
  Http_RuntimeProjectionRequest,
} from '../generated/openHttpContracts';
import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import type {
  RuntimeProjectionBuildPlan,
  RuntimeProjectionBuildRequest,
  RuntimeProjectionArtifactRequest,
  RuntimeProjectionStatus,
  RuntimeProjectionStatusRequest,
  TimelineProjectionArtifact,
} from './types';

export interface RuntimeProjectionOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

type RuntimeProjectionArtifactWire =
  HttpContractSuccessResponse<'tenant.runtime_projection_artifact.post'>;
type ContractErrorFactory = (status: number, payload: unknown) => Error;

export interface RuntimeProjectionsApi {
  status(
    input: RuntimeProjectionStatusRequest,
    options?: RuntimeProjectionOptions,
  ): Promise<RuntimeProjectionStatus>;
  build(
    input: RuntimeProjectionBuildRequest,
    options?: RuntimeProjectionOptions,
  ): Promise<RuntimeProjectionBuildPlan>;
  artifact(
    input: RuntimeProjectionArtifactRequest,
    options?: RuntimeProjectionOptions,
  ): Promise<TimelineProjectionArtifact>;
}

function artifactResponse(wire: RuntimeProjectionArtifactWire): TimelineProjectionArtifact {
  const root = requiredObject(wire, 'response');
  const schemaVersion = requiredString(root.schemaVersion, 'schemaVersion');
  if (schemaVersion !== 'frisket.timeline_projection_artifact.v1') {
    throw invalidArtifact('schemaVersion');
  }
  const projectionKind = requiredString(root.projectionKind, 'projectionKind');
  if (projectionKind !== 'timeline') throw invalidArtifact('projectionKind');

  const target = requiredObject(root.target, 'target');
  const params = requiredObject(root.params, 'params');
  const columns = requiredObject(root.columns, 'columns');
  const metrics = requiredObject(root.metrics, 'metrics');
  const rawItems = root.items;
  if (!Array.isArray(rawItems)) throw invalidArtifact('items');

  return {
    ...root,
    schemaVersion,
    generation: requiredString(root.generation, 'generation'),
    artifactId: requiredString(root.artifactId, 'artifactId'),
    projectionKind,
    target: {
      ...target,
      sheetId: requiredId(target.sheetId, 'target.sheetId'),
      dateColumnId: requiredId(target.dateColumnId, 'target.dateColumnId'),
      titleColumnId: requiredId(target.titleColumnId, 'target.titleColumnId'),
      ...optionalTargetId(target, 'caseColumnId', 'target.caseColumnId'),
    },
    params,
    columns: {
      ...columns,
      dateColumnId: requiredId(columns.dateColumnId, 'columns.dateColumnId'),
      titleColumnId: requiredId(columns.titleColumnId, 'columns.titleColumnId'),
      ...optionalId(columns, 'caseColumnId', 'columns.caseColumnId'),
    },
    metrics: {
      ...metrics,
      sourceRowCount: requiredCount(metrics.sourceRowCount, 'metrics.sourceRowCount'),
      timelineItemCount: requiredCount(metrics.timelineItemCount, 'metrics.timelineItemCount'),
    },
    items: rawItems.map((value, index) => {
      const item = requiredObject(value, `items[${index}]`);
      return {
        ...item,
        sourceRowId: requiredRowId(item.sourceRowId, `items[${index}].sourceRowId`),
        date: requiredStringValue(item.date, `items[${index}].date`),
        title: requiredStringValue(item.title, `items[${index}].title`),
        ...optionalId(item, 'caseId', `items[${index}].caseId`),
      };
    }),
  };
}

function invalidArtifact(field: string): Error {
  return new Error(`Invalid runtime projection artifact: ${field}`);
}

function requiredObject(value: unknown, field: string): Record<string, unknown> {
  if (!isObject(value)) throw invalidArtifact(field);
  return value;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function requiredStringValue(value: unknown, field: string): string {
  if (typeof value !== 'string') throw invalidArtifact(field);
  return value;
}

function requiredString(value: unknown, field: string): string {
  const result = requiredStringValue(value, field);
  if (!result.trim()) throw invalidArtifact(field);
  return result;
}

function requiredId(value: unknown, field: string): string {
  if (typeof value === 'string' && value.trim()) return value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  throw invalidArtifact(field);
}

function optionalId(
  object: Record<string, unknown>,
  key: string,
  field: string,
): Record<string, string | null> {
  if (!(key in object)) return {};
  if (object[key] === null) return { [key]: null };
  return { [key]: requiredId(object[key], field) };
}

function optionalTargetId(
  object: Record<string, unknown>,
  key: string,
  field: string,
): Record<string, string> {
  if (!(key in object) || object[key] === null) return {};
  return { [key]: requiredId(object[key], field) };
}

function requiredCount(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw invalidArtifact(field);
  }
  return value;
}

function requiredRowId(value: unknown, field: string): string | number {
  if (typeof value === 'string' && value.trim()) return value;
  if (typeof value === 'number' && Number.isInteger(value)) return value;
  throw invalidArtifact(field);
}

export function createRuntimeProjectionsApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): RuntimeProjectionsApi {
  return {
    status(input, options = {}) {
      return httpContract(
        'tenant.runtime_projection_status.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: input as Http_RuntimeProjectionRequest,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    build(input, options = {}) {
      return httpContract(
        'tenant.runtime_projection_build.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: input as Http_RuntimeProjectionBuildRequest,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    artifact(input, options = {}) {
      return httpContract(
        'tenant.runtime_projection_artifact.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: input as HttpRuntimeProjectionArtifactRequest,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        artifactResponse,
      );
    },
  };
}
