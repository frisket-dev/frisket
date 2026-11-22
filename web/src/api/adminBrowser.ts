import { httpContract, type HttpContractSuccessResponse, type RequestOptions } from './httpContract';

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
export type AdminUsersOrganization = AdminUsers['organizations'][number];
export type AdminOrganizationRole = AdminUsers['capabilities']['assignable_roles'][number];
export type AdminJob = AdminBrowserJobsWire['jobs'][number];
export type AdminJobs = AdminBrowserJobsWire;
type AdminJobActionResult = AdminBrowserCancelWire;
export type AdminErrorEvent = AdminBrowserErrorsWire['errors'][number];
export type AdminErrors = AdminBrowserErrorsWire;
export type AdminAuditEvent = AdminBrowserAuditWire['events'][number];
export type AdminAuditLog = AdminBrowserAuditWire;

/** Domain conveniences only; generated transport retains snake_case query keys. */
export interface AdminAuditFilters {
  limit?: number;
  organizationId?: string;
  projectId?: string;
  user?: string;
  action?: string;
}

function optionsFor(options: RequestOptions) {
  return {
    signal: options.signal,
  };
}

function auditQuery(filters: AdminAuditFilters) {
  const organizationId = Number(filters.organizationId?.trim());
  return {
    limit: filters.limit ?? 100,
    organization_id: Number.isFinite(organizationId) && organizationId > 0 ? organizationId : undefined,
    project_id: filters.projectId?.trim() || undefined,
    user: filters.user?.trim() || undefined,
    action: filters.action?.trim() || undefined,
  };
}

export function createAdminBrowserApi() {
  return {
    getAdminHealth(options: RequestOptions = {}): Promise<AdminHealth> {
      return httpContract(
        'outer.admin_browser_health.get',
        { ...optionsFor(options) },
      );
    },
    getAdminUsers(options: RequestOptions = {}): Promise<AdminUsers> {
      return httpContract(
        'outer.admin_browser_users.get',
        { ...optionsFor(options) },
      );
    },
    inviteAdminUser(
      organizationId: number,
      email: string,
      options: RequestOptions = {},
    ): Promise<AdminBrowserInviteWire> {
      return httpContract(
        'outer.admin_browser_invite_user.post',
        {
          body: { organization_id: organizationId, email },
          ...optionsFor(options),
        },
      );
    },
    updateAdminUserRole(
      organizationId: number,
      userId: number,
      role: AdminOrganizationRole,
      options: RequestOptions = {},
    ): Promise<AdminBrowserRoleWire> {
      return httpContract(
        'outer.admin_browser_update_user_role.patch',
        {
          pathParams: { user_id: userId }, body: { organization_id: organizationId, role },
          ...optionsFor(options),
        },
      );
    },
    removeAdminUser(
      organizationId: number,
      userId: number,
      options: RequestOptions = {},
    ): Promise<AdminBrowserRemoveWire> {
      return httpContract(
        'outer.admin_browser_remove_user.delete',
        {
          pathParams: { user_ref: String(userId) }, query: { organization_id: organizationId },
          ...optionsFor(options),
        },
      );
    },
    revokeAdminInvite(
      organizationId: number,
      email: string,
      options: RequestOptions = {},
    ): Promise<AdminBrowserRevokeWire> {
      return httpContract(
        'outer.admin_browser_revoke_invite.delete',
        {
          pathParams: { email }, query: { organization_id: organizationId },
          ...optionsFor(options),
        },
      );
    },
    getAdminJobs(options: RequestOptions = {}): Promise<AdminJobs> {
      return httpContract(
        'outer.admin_browser_jobs.get',
        { ...optionsFor(options) },
      );
    },
    cancelAdminJob(jobId: number, options: RequestOptions = {}): Promise<AdminJobActionResult> {
      return httpContract(
        'outer.admin_browser_cancel_job.post',
        { pathParams: { job_id: jobId }, ...optionsFor(options) },
      );
    },
    getAdminAuditLog(
      filters: AdminAuditFilters = {},
      options: RequestOptions = {},
    ): Promise<AdminAuditLog> {
      return httpContract(
        'outer.admin_browser_audit.get',
        { query: auditQuery(filters), ...optionsFor(options) },
      );
    },
    getAdminErrors(limit = 50, options: RequestOptions = {}): Promise<AdminErrors> {
      return httpContract(
        'outer.admin_browser_errors.get',
        { query: { limit }, ...optionsFor(options) },
      );
    },
  };
}
