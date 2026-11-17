import {
  httpContract,
  type HttpContractSuccessResponse,
  type RequestOptions,
} from '../httpContract';
import { apiErrorFromContract } from '../contractErrors';
import type { ProjectInfo } from '../types';

type ProjectWire = HttpContractSuccessResponse<'tenant.get_project.get'>;
type ProjectDeleteWire = HttpContractSuccessResponse<'tenant.delete_project.delete'>;
type ProjectListWire = HttpContractSuccessResponse<'tenant.list_projects.get'>;

class ProjectDeletionLifetimeChallenge extends Error {
  readonly projectRowId: number;

  constructor(projectRowId: number) {
    super('project deletion requires a lifetime-confirmation retry');
    this.name = 'ProjectDeletionLifetimeChallenge';
    this.projectRowId = projectRowId;
  }
}

interface ProjectUpdateInput {
  name?: string | null;
  description?: string | null;
  starred?: boolean | null;
  archived?: boolean | null;
}

function mapProject(wire: ProjectWire): ProjectInfo {
  return {
    id: wire.id,
    name: wire.name,
    description: wire.description,
    sensitive: wire.sensitive,
    starred: wire.starred ?? false,
    archived: wire.archived ?? false,
  };
}

function mapProjectList(wire: ProjectListWire): ProjectInfo[] {
  return wire.map((project) => ({
    id: project.id,
    name: project.name,
    description: project.description,
    sensitive: project.sensitive,
    updated_at: project.updated_at,
    pending_review_count: project.pending_review_count,
    starred: project.starred,
    archived: project.archived,
    role: project.role,
  }));
}

function mapDeletedProject(wire: ProjectDeleteWire): void {
  void wire;
  return undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function projectDeletionLifetimeChallenge(
  status: number,
  payload: unknown,
  projectId: string,
  confirmName: string,
): ProjectDeletionLifetimeChallenge | null {
  if (status !== 422 || !isRecord(payload) || !isRecord(payload.confirmation)) {
    return null;
  }

  const confirmation = payload.confirmation;
  const expectedKeys = ['action', 'project_id', 'project_row_id', 'confirm_name'];
  if (
    Object.keys(confirmation).length !== expectedKeys.length
    || !expectedKeys.every((key) => Object.prototype.hasOwnProperty.call(confirmation, key))
    || confirmation.action !== 'project.delete'
    || confirmation.project_id !== projectId
    || confirmation.confirm_name !== confirmName
    || typeof confirmation.project_row_id !== 'number'
    || !Number.isSafeInteger(confirmation.project_row_id)
    || confirmation.project_row_id <= 0
  ) {
    return null;
  }

  return new ProjectDeletionLifetimeChallenge(confirmation.project_row_id);
}

export function listProjects(
  options: RequestOptions = {},
): Promise<ProjectInfo[]> {
  return httpContract('tenant.list_projects.get', {
    signal: options.signal,
  }, mapProjectList);
}

export function getProjectContract(
  projectId: string,
  options: RequestOptions = {},
): Promise<ProjectInfo> {
  return httpContract('tenant.get_project.get', {
    pathParams: { pid: projectId },
    signal: options.signal,
  }, mapProject);
}

export function createProject(
  name: string,
  options: RequestOptions = {},
): Promise<ProjectInfo> {
  return httpContract('tenant.create_project.post', {
    body: { name },
    signal: options.signal,
  }, mapProject);
}

export function updateProject(
  projectId: string,
  input: ProjectUpdateInput,
  options: RequestOptions = {},
): Promise<ProjectInfo> {
  return httpContract('tenant.update_project.patch', {
    pathParams: { pid: projectId },
    body: input,
    signal: options.signal,
  }, mapProject);
}

export function deleteProject(
  projectId: string,
  confirmName: string,
  options: RequestOptions = {},
): Promise<void> {
  const request = (projectRowId?: number): Promise<void> => httpContract(
    'tenant.delete_project.delete',
    {
      pathParams: { pid: projectId },
      body: projectRowId === undefined
        ? { confirm_name: confirmName }
        : { confirm_name: confirmName, project_row_id: projectRowId },
      signal: options.signal,
      errorFactory: (status, payload) => {
        if (projectRowId === undefined) {
          const challenge = projectDeletionLifetimeChallenge(
            status,
            payload,
            projectId,
            confirmName,
          );
          if (challenge !== null) return challenge;
        }
        return apiErrorFromContract(status, payload);
      },
    },
    mapDeletedProject,
  );

  return request().catch((caught: unknown) => {
    if (!(caught instanceof ProjectDeletionLifetimeChallenge)) throw caught;
    return request(caught.projectRowId);
  });
}
