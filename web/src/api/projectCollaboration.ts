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

type InvitesWire = HttpContractSuccessResponse<'outer.list_project_invites.get'>;

function inviteList(wire: InvitesWire): ProjectInvite[] {
  return wire.invites;
}

export function createProjectCollaborationApi() {
  return {
    async listProjectMembers(
      projectId: string,
      options: RequestOptions = {},
    ): Promise<ProjectMember[]> {
      return httpContract(
        'outer.list_members.get',
        {
          pathParams: { pid: projectId },
          signal: options.signal,
        },
      );
    },

    async setProjectMember(
      projectId: string,
      email: string,
      role: ProjectRole,
      options: RequestOptions = {},
    ): Promise<ProjectMemberChange> {
      return httpContract(
        'outer.set_member.post',
        {
          pathParams: { pid: projectId },
          body: { email, role },
          signal: options.signal,
        },
      );
    },

    async removeProjectMember(
      projectId: string,
      email: string,
      options: RequestOptions = {},
    ): Promise<RemovalResult> {
      return httpContract(
        'outer.remove_member.delete',
        {
          pathParams: { pid: projectId, email },
          signal: options.signal,
        },
      );
    },

    async listProjectInvites(
      projectId: string,
      options: RequestOptions = {},
    ): Promise<ProjectInvite[]> {
      return httpContract(
        'outer.list_project_invites.get',
        {
          pathParams: { pid: projectId },
          signal: options.signal,
        },
        inviteList,
      );
    },

    async createProjectInvite(
      projectId: string,
      email: string,
      role: ProjectInviteRole,
      options: RequestOptions = {},
    ): Promise<ProjectInviteResponse> {
      return httpContract(
        'outer.create_project_invite.post',
        {
          pathParams: { pid: projectId },
          body: { email, role },
          signal: options.signal,
        },
      );
    },

    async revokeProjectInvite(
      projectId: string,
      inviteId: number | string,
      options: RequestOptions = {},
    ): Promise<{ ok: boolean; revoked: boolean }> {
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
