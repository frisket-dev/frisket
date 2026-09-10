import {
  Fragment,
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useState,
  type ReactNode,
} from 'react';
import { PanelSelect } from './PanelSelect';
import {
  ApiError,
  getAdminAuditLog,
  getAdminErrors,
  getAdminHealth,
  getAdminJobs,
  cancelAdminJob,
  getAdminOverview,
  getAdminUsers,
  getHealth,
  inviteAdminUser,
  removeAdminUser,
  revokeAdminInvite,
  updateAdminUserRole,
  type AdminAuditEvent,
  type AdminAuditFilters,
  type AdminAuditLog,
  type AdminErrorEvent,
  type AdminErrors,
  type AdminHealth,
  type AdminJob,
  type AdminJobs,
  type AdminOverview,
  type AdminOrgRole,
  type AdminUsers,
  type AdminUsersOrg,
} from '../api/open';
import { BrandLink } from './BrandLink';
import { useEditionModule } from '../editions/module';
import { DiagnosticsSettings } from '../settings/SettingsSections';

const ADMIN_LINKS = [
  { path: '/admin', label: 'Overview' },
  { path: '/admin/users', label: 'Users' },
  { path: '/admin/jobs', label: 'Jobs' },
  { path: '/admin/audit', label: 'Audit' },
  { path: '/admin/errors', label: 'Errors' },
  { path: '/admin/diagnostics', label: 'Diagnostics' },
] as const;

type AdminSection = 'overview' | 'users' | 'jobs' | 'audit' | 'errors' | 'diagnostics';

function currentAdminSection(): AdminSection {
  const section = window.location.pathname.split('/').filter(Boolean)[1] ?? 'overview';
  return ADMIN_LINKS.some(({ path }) => path === `/admin/${section}`)
    ? section as AdminSection
    : 'overview';
}

export function AdminNav() {
  const { routes } = useEditionModule();
  const contributed = routes
    .filter((route) => route.discoverableFrom === 'admin' && route.linkLabel)
    .map((route) => ({ path: route.path, label: route.linkLabel! }));
  const current = window.location.pathname;
  return (
    <nav className="admin-nav" aria-label="Admin sections" data-testid="admin-nav">
      {[...ADMIN_LINKS, ...contributed].map(({ path, label }) => (
        <a
          key={path}
          href={path}
          aria-current={current === path ? 'page' : undefined}
        >
          {label}
        </a>
      ))}
    </nav>
  );
}

export function AdminShell({ children }: { children: ReactNode }) {
  return (
    <div className="picker-screen" data-testid="admin-page">
      <div className="picker-card admin-card">
        <div className="picker-brand">
          <BrandLink size={18} />
          <span className="page-tag">admin</span>
        </div>
        <AdminNav />
        {children}
      </div>
    </div>
  );
}

function compactJson(value: unknown): string {
  if (value == null) return '—';
  if (typeof value === 'string') return value || '—';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function refString(refs: Record<string, unknown>, key: string): string | null {
  const value = refs[key];
  if (value === null || value === undefined || value === '') return null;
  return String(value);
}

function adminErrorContextLabel(event: AdminErrorEvent): string {
  const bits = [];
  if (event.project_id != null) bits.push(`project ${String(event.project_id)}`);
  if (event.sheet_id != null) bits.push(`sheet ${String(event.sheet_id)}`);
  if (event.run_id != null) bits.push(`run ${String(event.run_id)}`);
  if (event.job_id != null) bits.push(`job ${String(event.job_id)}`);
  return bits.join(' · ') || '—';
}

function adminErrorTraceLabel(event: AdminErrorEvent): string {
  const traceId = event.trace_id?.trim();
  if (traceId) return traceId;
  const requestId = event.context?.request_id;
  if (typeof requestId !== 'string' && typeof requestId !== 'number') return '—';
  const clean = String(requestId).trim();
  return clean || '—';
}

function safeAdminPath(value: string | null | undefined): string | null {
  const route = value?.trim();
  if (!route || !route.startsWith('/') || route.startsWith('//') || route.includes('\\')) {
    return null;
  }
  const path = route.split(/[?#]/, 1)[0];
  if (!path || !path.startsWith('/') || path.startsWith('//')) return null;
  return path;
}

function adminErrorEventLink(event: AdminErrorEvent): string | null {
  const route = safeAdminPath(event.route);
  if (route) return route;
  if (event.project_id != null) return `/p/${encodeURIComponent(String(event.project_id))}`;
  return null;
}

function adminJobRunLabel(job: AdminJob): string {
  const refs = job.refs ?? {};
  if (refs.run_id != null) return `run ${String(refs.run_id)}`;
  if (refs.source_id != null) return `source ${String(refs.source_id)}`;
  if (refs.project_id != null) return `project ${String(refs.project_id)}`;
  return '—';
}

function adminJobActionMetadata(job: AdminJob): {
  label: string;
  actionKind: string | null;
} | null {
  const actionName = job.action_name?.trim();
  const actionKind = job.action_kind?.trim() || null;
  const label = actionName || actionKind;
  if (!label) return null;
  return { label, actionKind };
}

function adminJobIdentity(job: AdminJob): string {
  const bits = [`job ${job.id}`];
  const refs = job.refs ?? {};
  const orgId = refString(refs, 'org_id');
  const projectId = refString(refs, 'project_id');
  const runId = refString(refs, 'run_id');
  const sourceId = refString(refs, 'source_id');
  const sheetId = refString(refs, 'sheet_id');
  if (orgId) bits.push(`org ${orgId}`);
  if (projectId) bits.push(`project ${projectId}`);
  if (runId) bits.push(`run ${runId}`);
  if (sourceId) bits.push(`source ${sourceId}`);
  if (sheetId) bits.push(`sheet ${sheetId}`);
  return bits.join(' · ');
}

function adminJobProjectHref(job: AdminJob): string | null {
  const projectId = refString(job.refs ?? {}, 'project_id');
  if (!projectId) return null;
  return `/p/${encodeURIComponent(projectId)}`;
}

function adminJobClipboardText(job: AdminJob): string {
  return compactJson({
    id: job.id,
    kind: job.kind,
    action_kind: job.action_kind ?? null,
    action_name: job.action_name ?? null,
    status: job.status,
    stalled: job.stalled ?? false,
    refs: job.refs ?? {},
    payload_ref: job.payload_ref ?? null,
    diagnostics: job.diagnostics ?? {},
    result_summary: job.result_summary ?? {},
    error: job.error ?? null,
  });
}

function copyAdminJob(job: AdminJob): void {
  void navigator.clipboard?.writeText(adminJobClipboardText(job)).catch(() => {});
}

function adminAuditSubject(event: AdminAuditEvent): string {
  if (event.object_type && event.object_id) return `${event.object_type} ${event.object_id}`;
  if (event.project_id != null) return `project ${String(event.project_id)}`;
  if (event.org_id != null) return `org ${String(event.org_id)}`;
  return event.source;
}

function adminAuditActor(event: AdminAuditEvent): string {
  if (event.actor_email) return event.actor_email;
  if (event.actor_user_id != null) return `user ${event.actor_user_id}`;
  return 'system';
}

function adminAuditScope(event: AdminAuditEvent): string {
  const bits = [];
  if (event.org_name || event.org_id != null) {
    bits.push(event.org_name ?? `org ${String(event.org_id)}`);
  }
  if (event.project_name || event.project_id != null) {
    bits.push(event.project_name ?? `project ${String(event.project_id)}`);
  }
  return bits.join(' · ') || '—';
}

type AdminState = {
  data: AdminOverview | null;
  auditLog: AdminAuditLog | null;
  jobs: AdminJobs | null;
  users: AdminUsers | null;
  errorsFeed: AdminErrors | null;
  health: AdminHealth | null;
  auditError: string | null;
  jobsError: string | null;
  usersError: string | null;
  errorsError: string | null;
  healthError: string | null;
  denied: boolean;
  error: string | null;
  /** From GET /api/health `posture.admin_configured`.
   *  null = not loaded yet / local tier has no posture field, so the denied
   *  screen falls back to the generic "not an admin" copy. */
  adminConfigured: boolean | null;
};

type AdminAction =
  | { type: 'overviewLoaded'; data: AdminOverview }
  | { type: 'overviewDenied' }
  | { type: 'overviewFailed'; message: string }
  | { type: 'healthLoaded'; adminConfigured: boolean | null }
  | { type: 'auditRefreshStarted' }
  | { type: 'auditLoaded'; auditLog: AdminAuditLog }
  | { type: 'auditFailed'; message: string }
  | { type: 'jobsRefreshStarted' }
  | { type: 'jobsLoaded'; jobs: AdminJobs }
  | { type: 'jobsFailed'; message: string }
  | { type: 'usersRefreshStarted' }
  | { type: 'usersLoaded'; users: AdminUsers }
  | { type: 'usersFailed'; message: string }
  | { type: 'errorsRefreshStarted' }
  | { type: 'errorsLoaded'; errorsFeed: AdminErrors }
  | { type: 'errorsFailed'; message: string }
  | { type: 'adminHealthRefreshStarted' }
  | { type: 'adminHealthLoaded'; health: AdminHealth }
  | { type: 'adminHealthFailed'; message: string };

const ADMIN_INITIAL_STATE: AdminState = {
  data: null,
  auditLog: null,
  jobs: null,
  users: null,
  errorsFeed: null,
  health: null,
  auditError: null,
  jobsError: null,
  usersError: null,
  errorsError: null,
  healthError: null,
  denied: false,
  error: null,
  adminConfigured: null,
};

function adminReducer(state: AdminState, action: AdminAction): AdminState {
  switch (action.type) {
    case 'overviewLoaded':
      return { ...state, data: action.data };
    case 'overviewDenied':
      return { ...state, denied: true };
    case 'overviewFailed':
      return { ...state, error: action.message };
    case 'healthLoaded':
      return { ...state, adminConfigured: action.adminConfigured };
    case 'auditRefreshStarted':
      return { ...state, auditError: null };
    case 'auditLoaded':
      return { ...state, auditLog: action.auditLog, auditError: null };
    case 'auditFailed':
      return { ...state, auditError: action.message };
    case 'jobsRefreshStarted':
      return { ...state, jobsError: null };
    case 'jobsLoaded':
      return { ...state, jobs: action.jobs, jobsError: null };
    case 'jobsFailed':
      return { ...state, jobsError: action.message };
    case 'usersRefreshStarted':
      return { ...state, usersError: null };
    case 'usersLoaded':
      return { ...state, users: action.users, usersError: null };
    case 'usersFailed':
      return { ...state, usersError: action.message };
    case 'errorsRefreshStarted':
      return { ...state, errorsError: null };
    case 'errorsLoaded':
      return { ...state, errorsFeed: action.errorsFeed, errorsError: null };
    case 'errorsFailed':
      return { ...state, errorsError: action.message };
    case 'adminHealthRefreshStarted':
      return { ...state, healthError: null };
    case 'adminHealthLoaded':
      return { ...state, health: action.health, healthError: null };
    case 'adminHealthFailed':
      return { ...state, healthError: action.message };
    default:
      return state;
  }
}

type PendingInviteConfirm = { orgId: number; orgName: string; email: string };

type AdminUsersPanelState = {
  selectedOrgId: string;
  inviteEmail: string;
  pendingInvite: PendingInviteConfirm | null;
  busy: string | null;
  actionError: string | null;
  notice: string | null;
};

type AdminUsersPanelAction =
  | { type: 'selectOrg'; orgId: string }
  | { type: 'inviteEmailChanged'; email: string }
  | { type: 'inviteConfirmRequested'; confirm: PendingInviteConfirm }
  | { type: 'inviteConfirmCancelled' }
  | { type: 'actionStarted'; label: string }
  | {
    type: 'actionSucceeded';
    notice: string;
    clearInviteEmail?: boolean;
  }
  | { type: 'actionFailed'; message: string };

const ADMIN_USERS_PANEL_INITIAL_STATE: AdminUsersPanelState = {
  selectedOrgId: '',
  inviteEmail: '',
  pendingInvite: null,
  busy: null,
  actionError: null,
  notice: null,
};

function adminUsersPanelReducer(
  state: AdminUsersPanelState,
  action: AdminUsersPanelAction,
): AdminUsersPanelState {
  switch (action.type) {
    case 'selectOrg':
      return { ...state, selectedOrgId: action.orgId };
    case 'inviteEmailChanged':
      return { ...state, inviteEmail: action.email };
    case 'inviteConfirmRequested':
      return { ...state, pendingInvite: action.confirm, actionError: null, notice: null };
    case 'inviteConfirmCancelled':
      return { ...state, pendingInvite: null };
    case 'actionStarted':
      return {
        ...state,
        busy: action.label,
        actionError: null,
        notice: null,
        pendingInvite: null,
      };
    case 'actionSucceeded':
      return {
        ...state,
        busy: null,
        actionError: null,
        notice: action.notice,
        inviteEmail: action.clearInviteEmail ? '' : state.inviteEmail,
      };
    case 'actionFailed':
      return { ...state, busy: null, actionError: action.message };
    default:
      return state;
  }
}

/** /admin — shared operational administration. The open team edition owns a
 * credit-free totals overview; a downstream composition may replace only that
 * overview block while retaining these health/jobs/users/audit panels. */
export function AdminPage() {
  const [state, dispatch] = useReducer(adminReducer, ADMIN_INITIAL_STATE);
  const {
    data,
    auditLog,
    jobs,
    users,
    errorsFeed,
    health,
    auditError,
    jobsError,
    usersError,
    errorsError,
    healthError,
    denied,
    error,
    adminConfigured,
  } = state;
  const { AdminOverviewSlot } = useEditionModule();
  const section = currentAdminSection();

  const loadAudit = useCallback((filters: AdminAuditFilters = {}) => {
    dispatch({ type: 'auditRefreshStarted' });
    return getAdminAuditLog(filters)
      .then((loaded) => dispatch({ type: 'auditLoaded', auditLog: loaded }))
      .catch((e: unknown) => {
        dispatch({ type: 'auditFailed', message: e instanceof Error ? e.message : String(e) });
      });
  }, []);

  const loadJobs = useCallback(() => {
    return getAdminJobs()
      .then((loaded) => dispatch({ type: 'jobsLoaded', jobs: loaded }))
      .catch((e: unknown) => {
        dispatch({ type: 'jobsFailed', message: e instanceof Error ? e.message : String(e) });
      });
  }, []);

  const loadAdminHealth = useCallback(() => {
    return getAdminHealth()
      .then((loaded) => dispatch({ type: 'adminHealthLoaded', health: loaded }))
      .catch((e: unknown) => {
        dispatch({
          type: 'adminHealthFailed',
          message: e instanceof Error ? e.message : String(e),
        });
      });
  }, []);

  const loadErrors = useCallback(() => {
    return getAdminErrors()
      .then((loaded) => dispatch({ type: 'errorsLoaded', errorsFeed: loaded }))
      .catch((e: unknown) => {
        dispatch({ type: 'errorsFailed', message: e instanceof Error ? e.message : String(e) });
      });
  }, []);

  const loadUsers = useCallback(() => {
    return getAdminUsers()
      .then((loaded) => dispatch({ type: 'usersLoaded', users: loaded }))
      .catch((e: unknown) => {
        dispatch({ type: 'usersFailed', message: e instanceof Error ? e.message : String(e) });
      });
  }, []);

  const refreshJobs = useCallback(() => {
    dispatch({ type: 'jobsRefreshStarted' });
    void loadJobs();
  }, [loadJobs]);

  const refreshUsers = useCallback(() => {
    dispatch({ type: 'usersRefreshStarted' });
    void loadUsers();
  }, [loadUsers]);

  const refreshErrors = useCallback(() => {
    dispatch({ type: 'errorsRefreshStarted' });
    void loadErrors();
  }, [loadErrors]);

  const refreshAdminHealth = useCallback(() => {
    dispatch({ type: 'adminHealthRefreshStarted' });
    void loadAdminHealth();
  }, [loadAdminHealth]);

  useEffect(() => {
    getAdminOverview()
      .then((loaded) => {
        dispatch({ type: 'overviewLoaded', data: loaded });
        if (section === 'audit') void loadAudit();
        if (section === 'jobs') void loadJobs();
        if (section === 'users') void loadUsers();
        if (section === 'errors') void loadErrors();
        if (section === 'diagnostics') void loadAdminHealth();
      })
      .catch((e: unknown) => {
        if (e instanceof ApiError && (e.status === 403 || e.status === 404)) {
          dispatch({ type: 'overviewDenied' });
          // Only a denied overview needs the public instance posture to
          // distinguish an unconfigured server from a non-admin account.
          void getHealth()
            .then((health) =>
              dispatch({
                type: 'healthLoaded',
                adminConfigured: health.posture?.admin_configured ?? null,
              }),
            )
            .catch(() => dispatch({ type: 'healthLoaded', adminConfigured: null }));
        } else {
          dispatch({ type: 'overviewFailed', message: e instanceof Error ? e.message : String(e) });
        }
      });
  }, [loadAdminHealth, loadAudit, loadErrors, loadJobs, loadUsers, section]);

  return (
    <AdminShell>
        {denied && adminConfigured === false ? (
          <div className="picker-empty" data-testid="admin-unconfigured">
            No owner is configured on this instance yet. Complete the instance
            setup from <code>/setup</code> using the code printed at startup.
          </div>
        ) : denied ? (
          <div className="picker-empty" data-testid="admin-denied">
            You&apos;re not an admin.
          </div>
        ) : error ? (
          <div className="picker-error">{error}</div>
        ) : data === null ? (
          <div className="picker-empty">Loading…</div>
        ) : (
          <>
            {section === 'overview' && (AdminOverviewSlot
              ? <AdminOverviewSlot overview={data} />
              : <TeamAdminOverview overview={data} />)}
            {section === 'users' && (
              <AdminUsersPanel users={users} error={usersError} onRefresh={refreshUsers} />
            )}
            {section === 'audit' && (
              <AdminAuditPanel auditLog={auditLog} error={auditError} onRefresh={loadAudit} />
            )}
            {section === 'jobs' && (
              <AdminJobsPanel jobs={jobs} error={jobsError} onRefresh={refreshJobs} />
            )}
            {section === 'errors' && (
              <AdminErrorsPanel feed={errorsFeed} error={errorsError} onRefresh={refreshErrors} />
            )}
            {section === 'diagnostics' && (
              <>
                <DiagnosticsSettings />
                <AdminHealthPanel
                  health={health}
                  error={healthError}
                  onRefresh={refreshAdminHealth}
                />
              </>
            )}
          </>
        )}
    </AdminShell>
  );
}

export function TeamAdminOverview({ overview }: { overview: Readonly<AdminOverview> }) {
  const totals = overview.totals;
  return (
    <section className="admin-jobs-panel" data-testid="team-admin-overview">
      <div className="admin-jobs-head">
        <strong>Team operations</strong>
      </div>
      <table className="admin-table" data-testid="team-admin-overview-table">
        <thead>
          <tr>
            <th>Members</th>
            <th>Projects</th>
            <th>Pending invites</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>{totals.users ?? '—'}</td>
            <td>{totals.projects ?? '—'}</td>
            <td>{totals.pending_invites ?? 0}</td>
          </tr>
        </tbody>
      </table>
    </section>
  );
}

function AdminUsersPanel({
  users,
  error,
  onRefresh,
}: {
  users: AdminUsers | null;
  error: string | null;
  onRefresh(): void;
}) {
  const orgs = users?.orgs ?? [];
  const assignableRoles = users?.capabilities.assignable_roles ?? [];
  const magicLinkTtlMinutes = users?.capabilities.magic_link_ttl_minutes;
  const inviteTtlDays = users?.capabilities.invite_ttl_days;
  const defaultOrgId = orgs[0]?.id ?? 0;
  const [formState, formDispatch] = useReducer(
    adminUsersPanelReducer,
    ADMIN_USERS_PANEL_INITIAL_STATE,
  );
  const {
    selectedOrgId,
    inviteEmail,
    pendingInvite,
    busy,
    actionError,
    notice,
  } = formState;
  const activeOrgId = Number(selectedOrgId || defaultOrgId);

  const userRows = useMemo(
    () => users?.orgs?.flatMap((org) => org.users.map((user) => ({ org, user }))) ?? [],
    [users],
  );
  const inviteRows = useMemo(
    () => users?.orgs?.flatMap((org) => (
      org.pending_invites.map((invite) => ({ org, invite }))
    )) ?? [],
    [users],
  );

  async function runAction(
    label: string,
    action: () => Promise<void>,
    options: { clearInviteEmail?: boolean } = {},
  ) {
    formDispatch({ type: 'actionStarted', label });
    try {
      await action();
      formDispatch({
        type: 'actionSucceeded',
        notice: label,
        clearInviteEmail: options.clearInviteEmail,
      });
      onRefresh();
    } catch (e) {
      formDispatch({ type: 'actionFailed', message: e instanceof Error ? e.message : String(e) });
    }
  }

  // Step 1 of 2: show a confirm panel before anything is sent, so an admin
  // inviting a batch of reporters sees exactly which org each email lands in
  // instead of silently minting per-person orgs.
  const submitInvite = (event: FormEvent) => {
    event.preventDefault();
    const email = inviteEmail.trim();
    if (!email || !activeOrgId) return;
    const org = orgs.find((item) => item.id === activeOrgId);
    formDispatch({
      type: 'inviteConfirmRequested',
      confirm: { orgId: activeOrgId, orgName: org?.name ?? `org ${activeOrgId}`, email },
    });
  };

  const confirmInvite = () => {
    if (!pendingInvite) return;
    const { orgId, email } = pendingInvite;
    void runAction('Invite queued', async () => {
      await inviteAdminUser(orgId, email);
    }, { clearInviteEmail: true });
  };

  const cancelInvite = () => formDispatch({ type: 'inviteConfirmCancelled' });

  const changeRole = (org: AdminUsersOrg, userId: number, role: AdminOrgRole) => {
    void runAction('Role updated', async () => {
      await updateAdminUserRole(org.id, userId, role);
    });
  };

  const revokeUser = (org: AdminUsersOrg, userId: number) => {
    void runAction('User access revoked', async () => {
      await removeAdminUser(org.id, userId);
    });
  };

  const revokeInvite = (org: AdminUsersOrg, email: string) => {
    void runAction('Invite revoked', async () => {
      await revokeAdminInvite(org.id, email);
    });
  };

  return (
    <section className="admin-users-panel" data-testid="admin-users-panel">
      <div className="admin-jobs-head">
        <strong>Users</strong>
        <span className="job-count" data-testid="admin-user-count">
          users {userRows.length}
        </span>
        <span className="job-count" data-testid="admin-invite-count">
          invites {inviteRows.length}
        </span>
        <button type="button" className="mini-btn" onClick={onRefresh}>
          Refresh
        </button>
      </div>

      <form className="admin-user-invite" data-testid="admin-invite-form" onSubmit={submitInvite}>
        <PanelSelect
          aria-label="Invite organization"
          className="form-input row-height-select"
          data-testid="admin-invite-org"
          value={String(activeOrgId || '')}
          onChange={(event) => formDispatch({ type: 'selectOrg', orgId: event.target.value })}
          disabled={orgs.length === 0 || busy !== null || pendingInvite !== null}
        >
          {orgs.map((org) => (
            <option key={org.id} value={org.id}>{org.name}</option>
          ))}
        </PanelSelect>
        <input
          aria-label="Invite email"
          className="form-input"
          data-testid="admin-invite-email"
          type="email"
          placeholder="email@example.com"
          value={inviteEmail}
          onChange={(event) => (
            formDispatch({ type: 'inviteEmailChanged', email: event.target.value })
          )}
          disabled={orgs.length === 0 || busy !== null || pendingInvite !== null}
        />
        <button
          type="submit"
          className="mini-btn"
          data-testid="admin-invite-submit"
          disabled={
            !inviteEmail.trim() || !activeOrgId || busy !== null || pendingInvite !== null
          }
        >
          Invite
        </button>
      </form>
      {pendingInvite && (
        <div className="account-note" data-testid="admin-invite-confirm">
          This adds <strong>{pendingInvite.email}</strong> to{' '}
          <strong>{pendingInvite.orgName}</strong>.
          <div className="admin-job-actions">
            <button
              type="button"
              className="mini-btn"
              data-testid="admin-invite-confirm-send"
              disabled={busy !== null}
              onClick={confirmInvite}
            >
              {busy ? 'Sending…' : 'Confirm & send invite'}
            </button>
            <button
              type="button"
              className="mini-btn"
              data-testid="admin-invite-confirm-cancel"
              disabled={busy !== null}
              onClick={cancelInvite}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      {notice && <div className="account-note" data-testid="admin-users-notice">{notice}</div>}
      {(error || actionError) && (
        <div className="picker-error" data-testid="admin-users-error">
          {actionError ?? `Could not load users: ${error}`}
        </div>
      )}

      <AdminUsersTable
        users={users}
        error={error}
        userRows={userRows}
        busy={busy}
        assignableRoles={assignableRoles}
        onChangeRole={changeRole}
        onRevoke={revokeUser}
      />

      <p className="picker-empty" data-testid="admin-invites-recovery-note">
        Invite emails carry a sign-in link valid for {magicLinkTtlMinutes ?? '—'}{' '}
        minute{magicLinkTtlMinutes === 1 ? '' : 's'}; the invite itself (below)
        stays open for {inviteTtlDays ?? '—'} day{inviteTtlDays === 1 ? '' : 's'}.
        If the link lapses, the invitee can request a fresh one from the sign-in
        page — no need to resend the invite.
      </p>
      <AdminInvitesTable
        users={users}
        error={error}
        inviteRows={inviteRows}
        busy={busy}
        onRevoke={revokeInvite}
      />
    </section>
  );
}

/** The org/email/role/actions table for existing users. Purely presentational:
 *  extracted from AdminUsersPanel so that panel focuses on
 *  the invite/create-org form logic. */
function AdminUsersTable({
  users,
  error,
  userRows,
  busy,
  assignableRoles,
  onChangeRole,
  onRevoke,
}: {
  users: AdminUsers | null;
  error: string | null;
  userRows: { org: AdminUsersOrg; user: AdminUsersOrg['users'][number] }[];
  busy: string | null;
  assignableRoles: readonly AdminOrgRole[];
  onChangeRole(org: AdminUsersOrg, userId: number, role: AdminOrgRole): void;
  onRevoke(org: AdminUsersOrg, userId: number): void;
}) {
  return (
    <table className="admin-table admin-users-table" data-testid="admin-users-table">
      <thead>
        <tr>
          <th>Org</th>
          <th>Email</th>
          <th>Role</th>
          <th>Joined</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {users === null ? (
          <tr>
            <td colSpan={5} className={error ? 'picker-error' : 'picker-empty'}>
              {error ? `Could not load users: ${error}` : 'Loading users…'}
            </td>
          </tr>
        ) : userRows.length === 0 ? (
          <tr><td colSpan={5} className="picker-empty">No users yet.</td></tr>
        ) : userRows.map(({ org, user }) => (
          <tr key={`${org.id}:${user.user_id}`}>
            <td>{org.name}</td>
            <td>{user.email}</td>
            <td>
              <PanelSelect
                aria-label={`Role for ${user.email}`}
                className="form-input admin-role-select"
                data-testid={`admin-user-role-${user.user_id}`}
                value={user.role}
                disabled={busy !== null}
                onChange={(event) => {
                  const role = assignableRoles.find(
                    (candidate) => candidate === event.target.value,
                  );
                  if (role) onChangeRole(org, user.user_id, role);
                }}
              >
                {assignableRoles.map((role) => (
                  <option key={role} value={role}>{role}</option>
                ))}
              </PanelSelect>
            </td>
            <td>{user.created_at ?? '—'}</td>
            <td>
              <button
                type="button"
                className="mini-btn"
                data-testid={`admin-user-revoke-${user.user_id}`}
                disabled={busy !== null}
                onClick={() => onRevoke(org, user.user_id)}
              >
                Remove membership
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** The pending-invite table. Purely presentational; extracted from
 *  AdminUsersPanel alongside AdminUsersTable. */
function AdminInvitesTable({
  users,
  error,
  inviteRows,
  busy,
  onRevoke,
}: {
  users: AdminUsers | null;
  error: string | null;
  inviteRows: { org: AdminUsersOrg; invite: AdminUsersOrg['pending_invites'][number] }[];
  busy: string | null;
  onRevoke(org: AdminUsersOrg, email: string): void;
}) {
  return (
    <table className="admin-table admin-invites-table" data-testid="admin-invites-table">
      <thead>
        <tr>
          <th>Org</th>
          <th>Pending invite</th>
          <th>Expires</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {users === null ? (
          <tr>
            <td colSpan={4} className={error ? 'picker-error' : 'picker-empty'}>
              {error ? `Could not load invites: ${error}` : 'Loading invites…'}
            </td>
          </tr>
        ) : inviteRows.length === 0 ? (
          <tr><td colSpan={4} className="picker-empty">No pending invites.</td></tr>
        ) : inviteRows.map(({ org, invite }) => (
          <tr key={`${org.id}:${invite.email}`}>
            <td>{org.name}</td>
            <td>{invite.email}</td>
            <td>{invite.expires_at ?? '—'}</td>
            <td>
              <button
                type="button"
                className="mini-btn"
                data-testid={`admin-invite-revoke-${invite.email}`}
                disabled={busy !== null}
                onClick={() => onRevoke(org, invite.email)}
              >
                Revoke
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function AdminAuditPanel({
  auditLog,
  error,
  onRefresh,
}: {
  auditLog: AdminAuditLog | null;
  error: string | null;
  onRefresh(filters?: AdminAuditFilters): Promise<void> | void;
}) {
  const [orgId, setOrgId] = useState('');
  const [projectId, setProjectId] = useState('');
  const [userFilter, setUserFilter] = useState('');
  const [action, setAction] = useState('');
  const events = auditLog?.events ?? [];
  const orgs = auditLog?.filters?.orgs ?? [];
  const allProjects = auditLog?.filters?.projects ?? [];
  const actions = auditLog?.filters?.actions ?? [];
  const projects = orgId
    ? allProjects.filter((project) => String(project.org_id) === orgId)
    : allProjects;

  const currentFilters = (): AdminAuditFilters => ({
    orgId: orgId || undefined,
    projectId: projectId || undefined,
    user: userFilter.trim() || undefined,
    action: action || undefined,
    limit: 100,
  });

  const submitFilters = (event: FormEvent) => {
    event.preventDefault();
    void onRefresh(currentFilters());
  };

  const clearFilters = () => {
    setOrgId('');
    setProjectId('');
    setUserFilter('');
    setAction('');
    void onRefresh({ limit: 100 });
  };

  const changeOrg = (value: string) => {
    setOrgId(value);
    const selectedProject = allProjects.find((project) => project.project_id === projectId);
    if (value && selectedProject && String(selectedProject.org_id) !== value) {
      setProjectId('');
    }
  };

  return (
    <section className="admin-audit" data-testid="admin-audit-panel">
      <div className="admin-jobs-head">
        <strong>Audit</strong>
        <span className="job-count" data-testid="admin-audit-count">
          events {events.length}
        </span>
        <button type="button" className="mini-btn" onClick={() => { void onRefresh(currentFilters()); }}>
          Refresh
        </button>
      </div>

      <form className="admin-audit-filters" data-testid="admin-audit-filters" onSubmit={submitFilters}>
        <PanelSelect
          aria-label="Audit organization"
          className="form-input"
          data-testid="admin-audit-org-filter"
          value={orgId}
          onChange={(event) => changeOrg(event.target.value)}
        >
          <option value="">All orgs</option>
          {orgs.map((org) => (
            <option key={org.id} value={org.id}>{org.name}</option>
          ))}
        </PanelSelect>
        <PanelSelect
          aria-label="Audit project"
          className="form-input"
          data-testid="admin-audit-project-filter"
          value={projectId}
          onChange={(event) => setProjectId(event.target.value)}
        >
          <option value="">All projects</option>
          {projects.map((project) => (
            <option key={`${project.org_id}:${project.project_id}`} value={project.project_id}>
              {project.name}
            </option>
          ))}
        </PanelSelect>
        <input
          aria-label="Audit user"
          className="form-input"
          data-testid="admin-audit-user-filter"
          placeholder="user or email"
          value={userFilter}
          onChange={(event) => setUserFilter(event.target.value)}
        />
        <PanelSelect
          aria-label="Audit action"
          className="form-input"
          data-testid="admin-audit-action-filter"
          value={action}
          onChange={(event) => setAction(event.target.value)}
        >
          <option value="">All actions</option>
          {actions.map((item) => (
            <option key={item} value={item}>{item}</option>
          ))}
        </PanelSelect>
        <button type="submit" className="mini-btn" data-testid="admin-audit-apply">
          Apply
        </button>
        <button
          type="button"
          className="mini-btn"
          data-testid="admin-audit-clear"
          onClick={clearFilters}
        >
          Clear
        </button>
      </form>

      {error && (
        <div className="picker-error" data-testid="admin-audit-error">
          Could not load audit: {error}
        </div>
      )}

      <table className="admin-table admin-audit-table" data-testid="admin-audit-table">
        <thead>
          <tr>
            <th>When</th>
            <th>Event</th>
            <th>Actor</th>
            <th>Scope</th>
            <th>Details</th>
          </tr>
        </thead>
        <tbody>
          {auditLog === null ? (
            <tr>
              <td colSpan={5} className={error ? 'picker-error' : 'picker-empty'}>
                {error ? `Could not load audit: ${error}` : 'Loading audit…'}
              </td>
            </tr>
          ) : events.length === 0 ? (
            <tr><td colSpan={5} className="picker-empty">No audit events.</td></tr>
          ) : events.map((event) => {
            const route = safeAdminPath(event.route);
            return (
              <tr key={event.id} data-testid={`admin-audit-row-${event.id}`}>
                <td>{event.created_at ?? '—'}</td>
                <td>
                  <span className={`admin-audit-category audit-${event.category}`}>
                    {event.category}
                  </span>
                  <div>{event.action}</div>
                  <div className="muted">{event.source}</div>
                </td>
                <td>{adminAuditActor(event)}</td>
                <td>{adminAuditScope(event)}</td>
                <td>
                  <details className="admin-audit-details" data-testid={`admin-audit-details-${event.id}`}>
                    <summary>{adminAuditSubject(event)}</summary>
                    <div className="admin-audit-detail-grid">
                      <span>object</span>
                      <strong>{event.object_type ?? '—'}</strong>
                      <span>id</span>
                      <strong>{event.object_id ?? '—'}</strong>
                      <span>detail</span>
                      <strong>{event.detail || '—'}</strong>
                      <span>route</span>
                      <strong>
                        {route ? <a className="mini-link" href={route}>Open</a> : '—'}
                      </strong>
                    </div>
                    <pre>{compactJson(event.context)}</pre>
                  </details>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

function AdminErrorsPanel({
  feed,
  error,
  onRefresh,
}: {
  feed: AdminErrors | null;
  error: string | null;
  onRefresh(): void;
}) {
  const events = feed?.errors ?? [];
  return (
    <section className="admin-errors" data-testid="admin-errors-panel">
      <div className="admin-jobs-head">
        <strong>Errors</strong>
        <span className="job-count job-failed" data-testid="admin-error-count">
          recent {events.length}
        </span>
        <button type="button" className="mini-btn" onClick={onRefresh}>
          Refresh
        </button>
      </div>
      <table className="admin-table admin-errors-table" data-testid="admin-errors-table">
        <thead>
          <tr>
            <th>Source</th>
            <th>Context</th>
            <th>Message</th>
            <th>Trace</th>
            <th>When</th>
            <th>Link</th>
          </tr>
        </thead>
        <tbody>
          {feed === null ? (
            <tr>
              <td colSpan={6} className={error ? 'picker-error' : 'picker-empty'}>
                {error ? `Could not load errors: ${error}` : 'Loading errors…'}
              </td>
            </tr>
          ) : events.length === 0 ? (
            <tr><td colSpan={6} className="picker-empty">No recent errors.</td></tr>
          ) : events.map((event) => {
            const link = adminErrorEventLink(event);
            return (
              <tr key={event.id}>
                <td>
                  <span className={`admin-error-kind kind-${event.kind}`}>
                    {event.kind}
                  </span>
                  <span className="admin-error-source">{event.source}</span>
                </td>
                <td>{adminErrorContextLabel(event)}</td>
                <td className="admin-error-message">
                  {event.name ? `${event.name}: ` : ''}
                  {event.message}
                  {event.bundle ? (
                    <details
                      className="admin-error-bundle"
                      data-testid={`admin-error-bundle-${event.record_id ?? event.id}`}
                    >
                      <summary>Diagnostic bundle</summary>
                      <pre>{compactJson(event.bundle)}</pre>
                    </details>
                  ) : null}
                </td>
                <td>{adminErrorTraceLabel(event)}</td>
                <td>{event.at ?? '—'}</td>
                <td>
                  {link ? (
                    <a href={link} className="mini-link">Open</a>
                  ) : '—'}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

// Ruling 4 ("a retry is a resume"): cancel is the only remediation the
// admin surface offers. Retry and Recover re-executed a user's work without
// passing the consent and cost gates that user's own doors pass — the only
// such doors in the product. Re-execution belongs to run.backfill (theirs,
// consented) or to the automatic budget-capped requeue/stale-lease sweeps.
type JobRemediation = 'cancel';

/** /admin health section — surfaces GET /api/admin/health (DB, blob-store,
 *  and run-queue probes plus active-run count). Kept deliberately small:
 *  ok/fail chips, queue counts, active runs — no history, no charts. */
export function AdminHealthPanel({
  health,
  error,
  onRefresh,
}: {
  health: AdminHealth | null;
  error: string | null;
  onRefresh(): void;
}) {
  const chip = (key: string, ok: boolean) => (
    <span
      key={key}
      className={`job-count${ok ? '' : ' job-failed'}`}
      data-testid={`admin-health-${key}`}
    >
      {key} {ok ? 'ok' : 'fail'}
    </span>
  );

  return (
    <section className="admin-health" data-testid="admin-health-panel">
      <div className="admin-jobs-head">
        <strong>Health</strong>
        {health === null ? (
          <span className="picker-empty" data-testid="admin-health-loading">
            {error ? `Could not load health: ${error}` : 'Loading…'}
          </span>
        ) : (
          <>
            {chip('overall', health.ok)}
            {chip('db', health.db.ok)}
            {chip('blob', health.blob_store.ok)}
            {chip('queue', health.queue.ok)}
            <span className="job-count" data-testid="admin-health-active-runs">
              active runs {health.active_runs}
            </span>
            {Object.entries(health.queue.counts).map(([key, n]) => (
              <span
                key={key}
                className="job-count"
                data-testid={`admin-health-queue-count-${key}`}
              >
                {key} {n}
              </span>
            ))}
          </>
        )}
        <button type="button" className="mini-btn" onClick={onRefresh}>
          Refresh
        </button>
      </div>
      {error && health !== null ? (
        <div className="picker-error" data-testid="admin-health-error">
          {error}
        </div>
      ) : null}
    </section>
  );
}

function AdminJobsPanel({
  jobs,
  error,
  onRefresh,
}: {
  jobs: AdminJobs | null;
  error: string | null;
  onRefresh(): void;
}) {
  const summary = jobs?.summary;
  const count = (key: 'queued' | 'running' | 'stalled' | 'failed') => summary?.[key] ?? 0;
  const [pending, setPending] = useState<{ id: number; action: JobRemediation } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const liveWorkers = jobs?.workers?.live ?? summary?.live_workers ?? 0;
  const noLiveWorker = jobs !== null && liveWorkers === 0 && count('queued') > 0;

  const remediate = useCallback(
    async (jobId: number, action: JobRemediation) => {
      setPending({ id: jobId, action });
      setActionError(null);
      try {
        await cancelAdminJob(jobId);
        onRefresh();
      } catch (e) {
        setActionError(
          `Could not ${action} job ${jobId}: ${e instanceof Error ? e.message : String(e)}`,
        );
      } finally {
        setPending(null);
      }
    },
    [onRefresh],
  );

  return (
    <section className="admin-jobs" data-testid="admin-jobs-panel">
      <div className="admin-jobs-head">
        <strong>Jobs</strong>
        {(['queued', 'running', 'stalled', 'failed'] as const).map((key) => (
          <span key={key} className={`job-count job-${key}`} data-testid={`admin-job-count-${key}`}>
            {key} {count(key)}
          </span>
        ))}
        <span
          className={`job-count job-workers${noLiveWorker ? ' job-stalled' : ''}`}
          data-testid="admin-worker-liveness"
        >
          {noLiveWorker ? 'no live worker' : `${liveWorkers} live worker(s)`}
        </span>
        <button type="button" className="mini-btn" onClick={onRefresh}>
          Refresh
        </button>
      </div>
      {actionError ? (
        <div className="picker-error" data-testid="admin-job-action-error">
          {actionError}
        </div>
      ) : null}
      <table className="admin-table admin-jobs-table" data-testid="admin-jobs-table">
        <thead>
          <tr>
            <th className="num">#</th>
            <th>Kind</th>
            <th>Status</th>
            <th>Worker</th>
            <th>Attempts</th>
            <th>IDs</th>
            <th>Timing</th>
            <th>Lease</th>
            <th>Error</th>
          </tr>
        </thead>
        <tbody>
          {jobs === null ? (
            <tr>
              <td colSpan={9} className={error ? 'picker-error' : 'picker-empty'}>
                {error ? `Could not load jobs: ${error}` : 'Loading jobs…'}
              </td>
            </tr>
          ) : jobs.jobs.length === 0 ? (
            <tr><td colSpan={9} className="picker-empty">No jobs yet.</td></tr>
          ) : jobs.jobs.map((job) => {
            const projectHref = adminJobProjectHref(job);
            const actionMetadata = adminJobActionMetadata(job);
            return (
              <Fragment key={job.id}>
                <tr key={job.id} data-testid={`admin-job-row-${job.id}`}>
                  <td className="num">{job.id}</td>
                  <td>
                    <div>{job.kind}</div>
                    {actionMetadata ? (
                      <div className="muted" data-testid={`admin-job-action-${job.id}`}>
                        <div>{actionMetadata.label}</div>
                        {actionMetadata.actionKind ? <div>{actionMetadata.actionKind}</div> : null}
                      </div>
                    ) : null}
                  </td>
                  <td>
                    {job.status}
                    {job.stalled ? (
                      <span className="job-count job-stalled" data-testid={`admin-job-stalled-${job.id}`}>
                        stalled
                      </span>
                    ) : null}
                  </td>
                  <td>
                    <div>{job.locked_by ?? '—'}</div>
                    <div className="muted">{job.locked_at ? `locked ${job.locked_at}` : ''}</div>
                  </td>
                  <td>{job.attempts}/{job.max_attempts}</td>
                  <td>
                    <div>{adminJobRunLabel(job)}</div>
                    <div className="muted">{adminJobIdentity(job)}</div>
                  </td>
                  <td>
                    <div>created {job.created_at ?? '—'}</div>
                    <div>available {job.available_at ?? '—'}</div>
                    <div>started {job.started_at ?? '—'}</div>
                    <div>finished {job.finished_at ?? '—'}</div>
                  </td>
                  <td>{job.lease_expires_at ?? '—'}</td>
                  <td>{job.error ?? '—'}</td>
                </tr>
                <tr key={`${job.id}:details`} className="admin-job-details-row">
                  <td colSpan={9}>
                    <div className="admin-job-actions">
                      {projectHref ? (
                        <a
                          href={projectHref}
                          className="mini-link"
                          data-testid={`admin-job-open-${job.id}`}
                        >
                          Open project
                        </a>
                      ) : (
                        <button type="button" className="mini-btn" disabled>
                          Open project
                        </button>
                      )}
                      <button
                        type="button"
                        className="mini-btn"
                        data-testid={`admin-job-copy-${job.id}`}
                        onClick={() => copyAdminJob(job)}
                      >
                        Copy JSON
                      </button>
                      <button
                        type="button"
                        className="mini-btn"
                        data-testid={`admin-job-cancel-${job.id}`}
                        disabled={
                          pending !== null ||
                          !(job.status === 'queued' || job.status === 'running')
                        }
                        onClick={() => void remediate(job.id, 'cancel')}
                      >
                        {pending?.id === job.id && pending.action === 'cancel' ? 'Cancelling…' : 'Cancel'}
                      </button>
                    </div>
                    <div className="admin-job-json">
                      <details open data-testid={`admin-job-refs-${job.id}`}>
                        <summary>Refs</summary>
                        <pre>{compactJson(job.refs ?? {})}</pre>
                      </details>
                      <details data-testid={`admin-job-diagnostics-${job.id}`}>
                        <summary>Diagnostics</summary>
                        <pre>{compactJson(job.diagnostics ?? {})}</pre>
                      </details>
                      <details data-testid={`admin-job-result-${job.id}`}>
                        <summary>Result summary</summary>
                        <pre>{compactJson(job.result_summary ?? {})}</pre>
                      </details>
                      <details data-testid={`admin-job-error-${job.id}`}>
                        <summary>Error detail</summary>
                        <pre>{compactJson(job.error ?? null)}</pre>
                      </details>
                    </div>
                  </td>
                </tr>
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
