import { httpContract, type HttpContractSuccessResponse, type RequestOptions } from './httpContract';
import type {
  ProjectInvite,
  ProjectInviteResponse,
  ProjectInviteRole,
  ProjectMember,
  ProjectMemberChange,
  ProjectRole,
  RemovalResult,
} from './types';

interface ProjectCollaborationApi {
  listMembers(
    projectId: string,
    options?: RequestOptions,
  ): Promise<ProjectMember[]>;
  setMember(
    projectId: string,
    email: string,
    role: ProjectRole,
    options?: RequestOptions,
  ): Promise<ProjectMemberChange>;
  removeMember(
    projectId: string,
    email: string,
    options?: RequestOptions,
  ): Promise<RemovalResult>;
  listInvites(
    projectId: string,
    options?: RequestOptions,
  ): Promise<ProjectInvite[]>;
  createInvite(
    projectId: string,
    email: string,
    role: ProjectInviteRole,
    options?: RequestOptions,
  ): Promise<ProjectInviteResponse>;
  revokeInvite(
    projectId: string,
    inviteId: number | string,
    options?: RequestOptions,
  ): Promise<{ ok: boolean; revoked: boolean }>;
}

type InvitesWire = HttpContractSuccessResponse<'outer.list_project_invites.get'>;

function inviteList(wire: InvitesWire): ProjectInvite[] {
  return wire.invites;
}

export function createProjectCollaborationApi(
): ProjectCollaborationApi {
  return {
    listMembers(projectId, options = {}) {
      return httpContract(
        'outer.list_members.get',
        {
          pathParams: { pid: projectId },
          signal: options.signal,
        },
      );
    },

    setMember(projectId, email, role, options = {}) {
      return httpContract(
        'outer.set_member.post',
        {
          pathParams: { pid: projectId },
          body: { email, role },
          signal: options.signal,
        },
      );
    },

    removeMember(projectId, email, options = {}) {
      return httpContract(
        'outer.remove_member.delete',
        {
          pathParams: { pid: projectId, email },
          signal: options.signal,
        },
      );
    },

    listInvites(projectId, options = {}) {
      return httpContract(
        'outer.list_project_invites.get',
        {
          pathParams: { pid: projectId },
          signal: options.signal,
        },
        inviteList,
      );
    },

    createInvite(projectId, email, role, options = {}) {
      return httpContract(
        'outer.create_project_invite.post',
        {
          pathParams: { pid: projectId },
          body: { email, role },
          signal: options.signal,
        },
      );
    },

    revokeInvite(projectId, inviteId, options = {}) {
      return httpContract(
        'outer.revoke_project_invite.delete',
        {
          pathParams: { pid: projectId, invite_id: inviteId as never },
          signal: options.signal,
        },
      );
    },
  };
}
