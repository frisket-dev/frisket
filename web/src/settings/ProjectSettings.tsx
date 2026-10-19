import { Button } from '../components/Button';
import { FormField, TextInput, TextArea } from '../components/FormControls';
import { type FormEvent, useCallback, useEffect, useReducer } from 'react';
import { AlertTriangle, Save, Trash2 } from 'lucide-react';
import {
  ApiError,
  listProjectInvites,
  listProjectMembers,
  revokeProjectInvite,
  setProjectMember,
  createProjectInvite,
  removeProjectMember,
  signOut,
  type ProjectInfo,
  type ProjectInvite,
  type ProjectInviteRole,
  type ProjectMember,
  type ProjectRole,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { canEditProject, canOwnProject, projectRole } from '../api/projectRole';
import { PanelSelect } from '../components/PanelSelect';
import { ConfirmTypeInput, PanelNotice } from '../components/PanelPrimitives';
import { navigate } from '../routes';
import { useLoader, errMsg } from './settingsSupport';
import { InlineStatus, SettingsTable } from './SettingsPrimitives';

const MEMBER_ROLES: ProjectRole[] = ['viewer', 'reviewer', 'editor', 'owner'];
const INVITE_ROLES: ProjectInviteRole[] = ['viewer', 'editor'];

function accessErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError && (error.status === 403 || error.status === 404)) {
    return fallback;
  }
  return errMsg(error);
}

function isFreshAuthError(error: unknown): boolean {
  return error instanceof ApiError &&
    error.status === 403 &&
    /fresh|reauth/i.test(error.message);
}

function formatDate(value: string | null | undefined): string {
  return value || '-';
}

function safeTestKey(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'unknown';
}

function memberKey(member: ProjectMember): string {
  return Number.isFinite(member.user_id) ? String(member.user_id) : safeTestKey(member.email);
}

interface ProjectGeneralFormState {
  name: string | null;
  description: string | null;
  confirmDelete: string;
  notice: string | null;
  mutationError: string | null;
  busy: boolean;
}

type ProjectGeneralFormAction =
  | { type: 'set_name'; value: string }
  | { type: 'set_description'; value: string }
  | { type: 'set_confirm_delete'; value: string }
  | { type: 'save_start' }
  | { type: 'save_success'; notice: string }
  | { type: 'save_error'; error: string }
  | { type: 'delete_start' }
  | { type: 'delete_error'; error: string };

function projectGeneralReducer(state: ProjectGeneralFormState, action: ProjectGeneralFormAction): ProjectGeneralFormState {
  switch (action.type) {
    case 'set_name': return { ...state, name: action.value };
    case 'set_description': return { ...state, description: action.value };
    case 'set_confirm_delete': return { ...state, confirmDelete: action.value };
    case 'save_start': return { ...state, busy: true, mutationError: null };
    case 'save_success': return { ...state, busy: false, notice: action.notice };
    case 'save_error': return { ...state, busy: false, notice: null, mutationError: action.error };
    case 'delete_start': return { ...state, busy: true, mutationError: null };
    case 'delete_error': return { ...state, busy: false, mutationError: action.error };
  }
}

const projectGeneralInitial: ProjectGeneralFormState = {
  name: null, description: null, confirmDelete: '', notice: null, mutationError: null, busy: false,
};

export function ProjectGeneralSettings({ project }: { project?: ProjectInfo | null }) {
  const { projectApi: api } = useWorkspaceStores();
  const details = useLoader<ProjectInfo>(() => api.getProject(), project?.id ?? '');
  const [form, dispatch] = useReducer(projectGeneralReducer, projectGeneralInitial);
  const nameValue = form.name ?? details.data?.name ?? project?.name ?? '';
  const descriptionValue = form.description ?? details.data?.description ?? project?.description ?? '';
  const current = details.data ?? project;
  const canEdit = canEditProject(current);
  const canOwner = canOwnProject(current);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!canEdit) return;
    dispatch({ type: 'save_start' });
    try {
      const saved = await api.updateCurrentProject({
        name: nameValue,
        description: descriptionValue,
      });
      details.setData(saved);
      dispatch({ type: 'save_success', notice: 'Project saved' });
    } catch (caught) {
      dispatch({ type: 'save_error', error: errMsg(caught) });
    }
  };

  return (
    <div className="content-stack" data-testid="project-general-settings">
      <InlineStatus error={details.error ?? form.mutationError} loading={details.loading} notice={form.notice} />
      {!canEdit && (
        <PanelNotice>
          Your project role can view project identity but cannot change it.
        </PanelNotice>
      )}
      <form className="content-stack" onSubmit={save}>
        <FormField label="Name" >

          <TextInput
            data-testid="project-name-input"
            value={nameValue}
            disabled={!canEdit || form.busy}
            onChange={(event) => dispatch({ type: 'set_name', value: event.currentTarget.value })}
          />
        </FormField>
        <FormField label="Description" >

          <TextArea
            data-testid="project-description-input"
            value={descriptionValue}
            disabled={!canEdit || form.busy}
            onChange={(event) => dispatch({ type: 'set_description', value: event.currentTarget.value })}
          />
        </FormField>
        <div className="settings-meta-list">
          <div><dt>Project</dt><dd>{current?.name ?? '-'}</dd></div>
          <div><dt>Project ID</dt><dd>{current?.id ?? '-'}</dd></div>
          <div><dt>Sensitive</dt><dd>{current?.sensitive ? 'Yes' : 'No'}</dd></div>
        </div>
        <div className="control-row">
          <Button className="btn-primary" type="submit" disabled={!canEdit || form.busy}>
            <Save size={14} /> Save
          </Button>
        </div>
      </form>
      {!canOwner && (
        <PanelNotice>
          Delete project is restricted to project owners.
        </PanelNotice>
      )}
      <form className="settings-danger-zone" onSubmit={(event) => {
        event.preventDefault();
        if (form.confirmDelete === current?.name && canOwner) {
          dispatch({ type: 'delete_start' });
          void api.deleteProject(current?.id, form.confirmDelete)
            .then(() => navigate({ kind: 'picker' }))
            .catch((caught) => dispatch({
              type: 'delete_error',
              error: caught instanceof ApiError && caught.status === 409
                ? 'This project still has runs in flight. Wait for them to finish or cancel them, then delete.'
                : errMsg(caught),
            }));
        }
      }}>
        <div className="settings-danger-header">
          <AlertTriangle size={16} />
          <div>
            <h2>Danger zone</h2>
            <p>Deleting this project permanently removes its sheets, files, run history, and settings. This cannot be undone.</p>
          </div>
        </div>
        <label>
          <span>Type {current?.name ?? 'the project name'} to delete</span>
          <ConfirmTypeInput
            data-testid="project-delete-confirm-input"
            id="frisket-project-delete-gate"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
            value={form.confirmDelete}
            disabled={!canOwner || form.busy}
            onChange={(value) => dispatch({ type: 'set_confirm_delete', value })}
          />
        </label>
        <Button
          className="btn-reject settings-danger-button"
          data-testid="project-delete-button"
          type="submit"
          disabled={!canOwner || form.busy || form.confirmDelete !== current?.name}
        >
          <Trash2 size={14} /> Delete
        </Button>
      </form>
    </div>
  );
}

interface ProjectAccessState {
  members: ProjectMember[] | null;
  invites: ProjectInvite[] | null;
  email: string;
  role: ProjectInviteRole;
  memberError: string | null;
  inviteError: string | null;
  actionError: string | null;
  notice: string | null;
  busy: string | null;
  freshAuthRequired: boolean;
  loadedProjectId: string | null;
}

type ProjectAccessAction =
  | { type: 'load_result'; members: ProjectMember[]; invites: ProjectInvite[]; memberError: string | null; inviteError: string | null; loadedProjectId: string }
  | { type: 'load_error'; error: string }
  | { type: 'action_start'; label: string }
  | { type: 'action_success'; notice: string }
  | { type: 'action_error'; error: string; freshAuthRequired: boolean }
  | { type: 'set_email'; value: string }
  | { type: 'set_role'; value: ProjectInviteRole }
  | { type: 'clear_email' };

function projectAccessReducer(state: ProjectAccessState, action: ProjectAccessAction): ProjectAccessState {
  switch (action.type) {
    case 'load_result':
      return { ...state, members: action.members, invites: action.invites, memberError: action.memberError, inviteError: action.inviteError, loadedProjectId: action.loadedProjectId };
    case 'load_error':
      return { ...state, actionError: action.error };
    case 'action_start':
      return { ...state, busy: action.label, actionError: null, notice: null, freshAuthRequired: false };
    case 'action_success':
      return { ...state, busy: null, notice: action.notice };
    case 'action_error':
      return { ...state, busy: null, actionError: action.error, freshAuthRequired: action.freshAuthRequired };
    case 'set_email':
      return { ...state, email: action.value };
    case 'set_role':
      return { ...state, role: action.value };
    case 'clear_email':
      return { ...state, email: '' };
  }
}

const projectAccessInitial: ProjectAccessState = {
  members: null, invites: null, email: '', role: 'viewer',
  memberError: null, inviteError: null, actionError: null, notice: null,
  busy: null, freshAuthRequired: false, loadedProjectId: null,
};

export function ProjectAccessSettings({ project, identityMode }: { project?: ProjectInfo | null; identityMode: boolean }) {
  const [state, dispatch] = useReducer(projectAccessReducer, projectAccessInitial);
  const projectId = project?.id ?? '';
  const canManageProject = ['owner', 'editor'].includes(projectRole(project));
  const canManageOwners = canOwnProject(project);
  const assignableRoles = canManageOwners
    ? MEMBER_ROLES
    : MEMBER_ROLES.filter((item) => item !== 'owner');
  const load = useCallback(async () => {
    if (!projectId || !identityMode) return;
    const [memberResult, inviteResult] = await Promise.allSettled([
      listProjectMembers(projectId),
      listProjectInvites(projectId),
    ]);
    dispatch({
      type: 'load_result',
      members: memberResult.status === 'fulfilled' ? memberResult.value : [],
      invites: inviteResult.status === 'fulfilled' ? inviteResult.value : [],
      memberError: memberResult.status === 'rejected'
        ? accessErrorMessage(memberResult.reason, 'You do not have permission to view project members.')
        : null,
      inviteError: inviteResult.status === 'rejected'
        ? accessErrorMessage(inviteResult.reason, 'You do not have permission to manage project invites.')
        : null,
      loadedProjectId: projectId,
    });
  }, [identityMode, projectId]);
  useEffect(() => {
    if (!identityMode) return undefined;
    let alive = true;
    void Promise.resolve().then(async () => {
      try {
        await load();
      } catch (caught) {
        if (alive) dispatch({ type: 'load_error', error: errMsg(caught) });
      }
    });
    return () => {
      alive = false;
    };
  }, [identityMode, load]);
  if (!identityMode) {
    return <div className="panel-empty">Project access is available on hosted workspaces.</div>;
  }
  const loading = state.loadedProjectId !== projectId;
  const runAction = async (label: string, action: () => Promise<void>) => {
    if (state.busy !== null || !canManageProject) return;
    dispatch({ type: 'action_start', label });
    try {
      await action();
      dispatch({ type: 'action_success', notice: label });
    } catch (caught) {
      dispatch({
        type: 'action_error',
        error: isFreshAuthError(caught)
          ? 'Sign in again to change project access.'
          : accessErrorMessage(caught, 'You do not have permission to manage project access.'),
        freshAuthRequired: isFreshAuthError(caught),
      });
    }
  };
  return (
    <div className="content-stack" data-testid="project-access-settings">
      <InlineStatus error={state.actionError} loading={loading} notice={state.notice} />
      {state.freshAuthRequired && (
        <Button

          type="button"
          data-testid="project-access-fresh-auth"
          onClick={() => void signOut().finally(() => window.location.assign('/'))}
        >
          Sign in again
        </Button>
      )}
      {!canManageProject && (
        <PanelNotice>
          Your project role can view access state but cannot change members or invites.
        </PanelNotice>
      )}
      {canManageProject && (
        <form className="form-inline" onSubmit={(event) => {
          event.preventDefault();
          const inviteEmail = state.email.trim();
          if (!inviteEmail) return;
          void runAction('Invite created', async () => {
            await createProjectInvite(projectId, inviteEmail, state.role);
            dispatch({ type: 'clear_email' });
            await load();
          });
        }}>
          <TextInput
            aria-label="Invite email"
            data-testid="project-invite-email"
            placeholder="Email"
            value={state.email}
            disabled={state.busy !== null}
            onChange={(event) => dispatch({ type: 'set_email', value: event.currentTarget.value })}
          />
          <PanelSelect
            data-testid="project-invite-role"
            value={state.role}
            disabled={state.busy !== null}
            onChange={(event) => dispatch({ type: 'set_role', value: event.currentTarget.value as ProjectInviteRole })}
          >
            {INVITE_ROLES.map((item) => <option key={item} value={item}>{item}</option>)}
          </PanelSelect>
          <Button className="btn-primary" data-testid="project-invite-submit" type="submit" disabled={state.busy !== null || !state.email.trim()}>Invite</Button>
        </form>
      )}
      <InlineStatus error={state.memberError} />
      <SettingsTable
        headers={['Member', 'Role', '']}
        rows={(state.members ?? []).map((member) => [
          member.email,
          <PanelSelect
            key={`${memberKey(member)}-role`}
            data-testid={`project-member-role-${memberKey(member)}`}
            value={member.role}
            disabled={
              state.busy !== null ||
              !canManageProject ||
              (member.role === 'owner' && !canManageOwners)
            }
            onChange={(event) => void runAction('Role updated', async () => {
              await setProjectMember(projectId, member.email, event.currentTarget.value as ProjectRole);
              await load();
            })}
          >
            {assignableRoles.includes(member.role)
              ? null
              : <option key={member.role} value={member.role}>{member.role}</option>}
            {assignableRoles.map((item) => <option key={item} value={item}>{item}</option>)}
          </PanelSelect>,
          <button
            key={`${memberKey(member)}-remove`}
            className="icon-btn"
            data-testid={`project-member-remove-${memberKey(member)}`}
            type="button"
            aria-label={`Remove ${member.email}`}
            title={`Remove ${member.email}`}
            disabled={
              state.busy !== null ||
              !canManageProject ||
              (member.role === 'owner' && !canManageOwners)
            }
            onClick={() => void runAction('Member removed', async () => {
              await removeProjectMember(projectId, member.email);
              await load();
            })}
          >
            <Trash2 size={14} />
          </button>,
        ])}
        empty="No members"
      />
      <InlineStatus error={state.inviteError} />
      <SettingsTable
        headers={['Pending invite', 'Role', 'Expires', '']}
        rows={(state.invites ?? []).map((invite) => [
          invite.email,
          invite.role,
          formatDate(invite.expires_at),
          <Button
            key={invite.id}

            data-testid={`project-invite-revoke-${invite.id}`}
            type="button"
            disabled={state.busy !== null || !canManageProject}
            onClick={() => void runAction('Invite revoked', async () => {
              await revokeProjectInvite(projectId, invite.id);
              await load();
            })}
          >
            Revoke
          </Button>,
        ])}
        empty="No pending invites"
      />
    </div>
  );
}
