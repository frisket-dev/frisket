import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import type {
  ProjectInvite,
  ProjectInviteResponse,
  ProjectInviteRole,
  ProjectMember,
  ProjectMemberChange,
  ProjectRole,
  RemovalResult,
} from './types';

export interface ProjectCollaborationOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface ProjectCollaborationApi {
  listMembers(
    projectId: string,
    options?: ProjectCollaborationOptions,
  ): Promise<ProjectMember[]>;
  setMember(
    projectId: string,
    email: string,
    role: ProjectRole,
    options?: ProjectCollaborationOptions,
  ): Promise<ProjectMemberChange>;
  removeMember(
    projectId: string,
    email: string,
    options?: ProjectCollaborationOptions,
  ): Promise<RemovalResult>;
  listInvites(
    projectId: string,
    options?: ProjectCollaborationOptions,
  ): Promise<ProjectInvite[]>;
  createInvite(
    projectId: string,
    email: string,
    role: ProjectInviteRole,
    options?: ProjectCollaborationOptions,
  ): Promise<ProjectInviteResponse>;
  revokeInvite(
    projectId: string,
    inviteId: number | string,
    options?: ProjectCollaborationOptions,
  ): Promise<{ ok: boolean; revoked: boolean }>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
type InvitesWire = HttpContractSuccessResponse<'outer.list_project_invites.get'>;

function inviteList(wire: InvitesWire): ProjectInvite[] {
  return wire.invites;
}

export function createProjectCollaborationApi(
  errorFactory: ContractErrorFactory,
): ProjectCollaborationApi {
  return {
    listMembers(projectId, options = {}) {
      return httpContract(
        'outer.list_members.get',
        {
          pathParams: { pid: projectId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    setMember(projectId, email, role, options = {}) {
      return httpContract(
        'outer.set_member.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: { email, role },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    removeMember(projectId, email, options = {}) {
      return httpContract(
        'outer.remove_member.delete',
        {
          pathParams: { pid: projectId, email },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    listInvites(projectId, options = {}) {
      return httpContract(
        'outer.list_project_invites.get',
        {
          pathParams: { pid: projectId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        inviteList,
      );
    },

    createInvite(projectId, email, role, options = {}) {
      return httpContract(
        'outer.create_project_invite.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: { email, role },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    revokeInvite(projectId, inviteId, options = {}) {
      return httpContract(
        'outer.revoke_project_invite.delete',
        {
          pathParams: { pid: projectId, invite_id: inviteId as never },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}
