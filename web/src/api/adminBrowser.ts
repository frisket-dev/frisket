import { httpContract, type HttpContractSuccessResponse } from './httpContract';

type ContractErrorFactory = (status: number, payload: unknown) => Error;
type AdminBrowserHealthWire =
  HttpContractSuccessResponse<'outer.admin_browser_health.get'>;
type AdminBrowserUsersWire =
  HttpContractSuccessResponse<'outer.admin_browser_users.get'>;
type AdminBrowserInviteWire =
  HttpContractSuccessResponse<'outer.admin_browser_invite_user.post'>;
type AdminBrowserRoleWire =
  HttpContractSuccessResponse<'outer.admin_browser_update_user_role.patch'>;
type AdminBrowserRemoveWire =
  HttpContractSuccessResponse<'outer.admin_browser_remove_user.delete'>;
type AdminBrowserRevokeWire =
  HttpContractSuccessResponse<'outer.admin_browser_revoke_invite.delete'>;
type AdminBrowserJobsWire =
  HttpContractSuccessResponse<'outer.admin_browser_jobs.get'>;
type AdminBrowserCancelWire =
  HttpContractSuccessResponse<'outer.admin_browser_cancel_job.post'>;
type AdminBrowserAuditWire =
  HttpContractSuccessResponse<'outer.admin_browser_audit.get'>;
type AdminBrowserErrorsWire =
  HttpContractSuccessResponse<'outer.admin_browser_errors.get'>;

export type AdminHealth = AdminBrowserHealthWire;
export type AdminUsers = AdminBrowserUsersWire;
export type AdminUsersOrg = AdminUsers['orgs'][number];
export type AdminUser = AdminUsersOrg['users'][number];
export type AdminInvite = AdminUsersOrg['pending_invites'][number];
export type AdminOrgRole = AdminUsers['capabilities']['assignable_roles'][number];
export type AdminJob = AdminBrowserJobsWire['jobs'][number];
export type AdminJobs = AdminBrowserJobsWire;
export type AdminJobActionResult = AdminBrowserCancelWire;
export type AdminInviteResult = AdminBrowserInviteWire;
export type AdminRoleUpdateResult = AdminBrowserRoleWire;
export type AdminRemoveUserResult = AdminBrowserRemoveWire;
export type AdminRevokeInviteResult = AdminBrowserRevokeWire;
export type AdminErrorEvent = AdminBrowserErrorsWire['errors'][number];
export type AdminErrors = AdminBrowserErrorsWire;
export type AdminAuditEvent = AdminBrowserAuditWire['events'][number];
export type AdminAuditCategory = AdminAuditEvent['category'];
export type AdminAuditLog = AdminBrowserAuditWire;

/** Domain conveniences only; generated transport retains snake_case query keys. */
export interface AdminAuditFilters {
  limit?: number;
  orgId?: string;
  projectId?: string;
  user?: string;
  action?: string;
}

export interface AdminBrowserOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface AdminBrowserApi {
  getAdminHealth(options?: AdminBrowserOptions): Promise<AdminHealth>;
  getAdminUsers(options?: AdminBrowserOptions): Promise<AdminUsers>;
  inviteAdminUser(
    orgId: number,
    email: string,
    options?: AdminBrowserOptions,
  ): Promise<AdminBrowserInviteWire>;
  updateAdminUserRole(
    orgId: number,
    userId: number,
    role: AdminOrgRole,
    options?: AdminBrowserOptions,
  ): Promise<AdminBrowserRoleWire>;
  removeAdminUser(
    orgId: number,
    userId: number,
    options?: AdminBrowserOptions,
  ): Promise<AdminBrowserRemoveWire>;
  revokeAdminInvite(
    orgId: number,
    email: string,
    options?: AdminBrowserOptions,
  ): Promise<AdminBrowserRevokeWire>;
  getAdminJobs(options?: AdminBrowserOptions): Promise<AdminJobs>;
  cancelAdminJob(
    jobId: number,
    options?: AdminBrowserOptions,
  ): Promise<AdminJobActionResult>;
  getAdminAuditLog(
    filters?: AdminAuditFilters,
    options?: AdminBrowserOptions,
  ): Promise<AdminAuditLog>;
  getAdminErrors(
    limit?: number,
    options?: AdminBrowserOptions,
  ): Promise<AdminErrors>;
}

function optionsFor(options: AdminBrowserOptions) {
  return {
    signal: options.signal,
    headers: options.headers,
  };
}

function auditQuery(filters: AdminAuditFilters) {
  const orgId = Number(filters.orgId?.trim());
  return {
    limit: filters.limit ?? 100,
    org_id: Number.isFinite(orgId) && orgId > 0 ? orgId : undefined,
    project_id: filters.projectId?.trim() || undefined,
    user: filters.user?.trim() || undefined,
    action: filters.action?.trim() || undefined,
  };
}

export function createAdminBrowserApi(
  errorFactory: ContractErrorFactory,
): AdminBrowserApi {
  return {
    getAdminHealth(options = {}) {
      return httpContract(
        'outer.admin_browser_health.get',
        { pathParams: {}, query: {}, errorFactory, ...optionsFor(options) },
      );
    },
    getAdminUsers(options = {}) {
      return httpContract(
        'outer.admin_browser_users.get',
        { pathParams: {}, query: {}, errorFactory, ...optionsFor(options) },
      );
    },
    inviteAdminUser(orgId, email, options = {}) {
      return httpContract(
        'outer.admin_browser_invite_user.post',
        {
          pathParams: {}, query: {}, body: { org_id: orgId, email }, errorFactory,
          ...optionsFor(options),
        },
      );
    },
    updateAdminUserRole(orgId, userId, role, options = {}) {
      return httpContract(
        'outer.admin_browser_update_user_role.patch',
        {
          pathParams: { user_id: userId }, query: {}, body: { org_id: orgId, role }, errorFactory,
          ...optionsFor(options),
        },
      );
    },
    removeAdminUser(orgId, userId, options = {}) {
      return httpContract(
        'outer.admin_browser_remove_user.delete',
        {
          pathParams: { user_ref: String(userId) }, query: { org_id: orgId }, errorFactory,
          ...optionsFor(options),
        },
      );
    },
    revokeAdminInvite(orgId, email, options = {}) {
      return httpContract(
        'outer.admin_browser_revoke_invite.delete',
        {
          pathParams: { email }, query: { org_id: orgId }, errorFactory,
          ...optionsFor(options),
        },
      );
    },
    getAdminJobs(options = {}) {
      return httpContract(
        'outer.admin_browser_jobs.get',
        { pathParams: {}, query: {}, errorFactory, ...optionsFor(options) },
      );
    },
    cancelAdminJob(jobId, options = {}) {
      return httpContract(
        'outer.admin_browser_cancel_job.post',
        { pathParams: { job_id: jobId }, query: {}, errorFactory, ...optionsFor(options) },
      );
    },
    getAdminAuditLog(filters = {}, options = {}) {
      return httpContract(
        'outer.admin_browser_audit.get',
        { pathParams: {}, query: auditQuery(filters), errorFactory, ...optionsFor(options) },
      );
    },
    getAdminErrors(limit = 50, options = {}) {
      return httpContract(
        'outer.admin_browser_errors.get',
        { pathParams: {}, query: { limit }, errorFactory, ...optionsFor(options) },
      );
    },
  };
}
