import { Button } from '../components/Button';
import { FormField, TextInput, TextArea } from '../components/FormControls';
import { type FormEvent, useState } from 'react';
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
  type ProjectInviteRole,
  type ProjectMember,
  type ProjectRole,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { canEditProject, canOwnProject, projectRole } from '../api/projectRole';
import { PanelSelect } from '../components/PanelSelect';
import { ConfirmTypeInput, PanelNotice } from '../components/PanelPrimitives';
import { navigate } from '../routes';
import { useAction } from '../hooks/useAction';
import { useLoader } from '../hooks/useLoader';
import { errorMessage } from '../hooks/errorMessage';
import { InlineStatus, SettingsTable } from './SettingsPrimitives';

const MEMBER_ROLES: ProjectRole[] = ['viewer', 'reviewer', 'editor', 'owner'];
const INVITE_ROLES: ProjectInviteRole[] = ['viewer', 'editor'];

function accessErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError && (error.status === 403 || error.status === 404)) {
    return fallback;
  }
  return errorMessage(error);
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

export function ProjectGeneralSettings({ project }: { project?: ProjectInfo | null }) {
  const { projectApi: api } = useWorkspaceStores();
  const details = useLoader<ProjectInfo>(() => api.getProject(), [project?.id ?? ''], { keepPreviousData: true });
  const [name, setName] = useState<string | null>(null);
  const [description, setDescription] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const mutation = useAction();
  const busy = mutation.busy;
  const nameValue = name ?? details.data?.name ?? project?.name ?? '';
  const descriptionValue = description ?? details.data?.description ?? project?.description ?? '';
  const current = details.data ?? project;
  const canEdit = canEditProject(current);
  const canOwner = canOwnProject(current);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!canEdit) return;
    await mutation.run(async () => {
      try {
        details.setData(await api.updateCurrentProject({
          name: nameValue,
          description: descriptionValue,
        }));
      } catch (caught) {
        setNotice(null);
        throw caught;
      }
      setNotice('Project saved');
    });
  };

  return (
    <div className="content-stack" data-testid="project-general-settings">
      <InlineStatus error={details.error ?? mutation.error} loading={details.loading} notice={notice} />
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
            disabled={!canEdit || busy}
            onChange={(event) => setName(event.currentTarget.value)}
          />
        </FormField>
        <FormField label="Description" >

          <TextArea
            data-testid="project-description-input"
            value={descriptionValue}
            disabled={!canEdit || busy}
            onChange={(event) => setDescription(event.currentTarget.value)}
          />
        </FormField>
        <div className="settings-meta-list">
          <div><dt>Project</dt><dd>{current?.name ?? '-'}</dd></div>
          <div><dt>Project ID</dt><dd>{current?.id ?? '-'}</dd></div>
          <div><dt>Sensitive</dt><dd>{current?.sensitive ? 'Yes' : 'No'}</dd></div>
        </div>
        <div className="control-row">
          <Button className="btn-primary" type="submit" disabled={!canEdit || busy}>
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
        if (confirmDelete === current?.name && canOwner) {
          void mutation.run(async () => {
            try {
              await api.deleteProject(current?.id, confirmDelete);
            } catch (caught) {
              if (caught instanceof ApiError && caught.status === 409) {
                throw new Error('This project still has runs in flight. Wait for them to finish or cancel them, then delete.', { cause: caught });
              }
              throw caught;
            }
            navigate({ kind: 'picker' });
          });
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
            value={confirmDelete}
            disabled={!canOwner || busy}
            onChange={setConfirmDelete}
          />
        </label>
        <Button
          className="btn-reject settings-danger-button"
          data-testid="project-delete-button"
          type="submit"
          disabled={!canOwner || busy || confirmDelete !== current?.name}
        >
          <Trash2 size={14} /> Delete
        </Button>
      </form>
    </div>
  );
}

interface ProjectAccessAction {
  busy: string | null;
  error: string | null;
  notice: string | null;
  freshAuthRequired: boolean;
}

const ACCESS_ACTION_IDLE: ProjectAccessAction = { busy: null, error: null, notice: null, freshAuthRequired: false };

export function ProjectAccessSettings({ project, identityMode }: { project?: ProjectInfo | null; identityMode: boolean }) {
  const projectId = project?.id ?? '';
  const canManageProject = ['owner', 'editor'].includes(projectRole(project));
  const canManageOwners = canOwnProject(project);
  const assignableRoles = canManageOwners
    ? MEMBER_ROLES
    : MEMBER_ROLES.filter((item) => item !== 'owner');
  const [email, setEmail] = useState('');
  const [role, setRole] = useState<ProjectInviteRole>('viewer');
  const [action, setAction] = useState<ProjectAccessAction>(ACCESS_ACTION_IDLE);
  const access = useLoader(async () => {
    const [memberResult, inviteResult] = await Promise.allSettled([
      listProjectMembers(projectId),
      listProjectInvites(projectId),
    ]);
    return {
      members: memberResult.status === 'fulfilled' ? memberResult.value : [],
      invites: inviteResult.status === 'fulfilled' ? inviteResult.value : [],
      memberError: memberResult.status === 'rejected'
        ? accessErrorMessage(memberResult.reason, 'You do not have permission to view project members.')
        : null,
      inviteError: inviteResult.status === 'rejected'
        ? accessErrorMessage(inviteResult.reason, 'You do not have permission to manage project invites.')
        : null,
    };
  }, [identityMode, projectId], { keepPreviousData: true, enabled: identityMode && projectId !== '' });
  const load = access.reload;
  if (!identityMode) {
    return <div className="panel-empty">Project access is available on Team and Cloud servers.</div>;
  }
  const loading = access.data === null || access.loading;
  const members = access.data?.members ?? null;
  const invites = access.data?.invites ?? null;
  const runAction = async (label: string, task: () => Promise<void>) => {
    if (action.busy !== null || !canManageProject) return;
    setAction({ ...ACCESS_ACTION_IDLE, busy: label });
    try {
      await task();
      setAction({ ...ACCESS_ACTION_IDLE, notice: label });
    } catch (caught) {
      const freshAuthRequired = isFreshAuthError(caught);
      setAction({
        ...ACCESS_ACTION_IDLE,
        error: freshAuthRequired
          ? 'Sign in again to change project access.'
          : accessErrorMessage(caught, 'You do not have permission to manage project access.'),
        freshAuthRequired,
      });
    }
  };
  return (
    <div className="content-stack" data-testid="project-access-settings">
      <InlineStatus error={action.error ?? access.error} loading={loading} notice={action.notice} />
      {action.freshAuthRequired && (
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
          const inviteEmail = email.trim();
          if (!inviteEmail) return;
          void runAction('Invite created', async () => {
            await createProjectInvite(projectId, inviteEmail, role);
            setEmail('');
            await load();
          });
        }}>
          <TextInput
            aria-label="Invite email"
            data-testid="project-invite-email"
            placeholder="Email"
            value={email}
            disabled={action.busy !== null}
            onChange={(event) => setEmail(event.currentTarget.value)}
          />
          <PanelSelect
            data-testid="project-invite-role"
            value={role}
            disabled={action.busy !== null}
            onChange={(event) => setRole(event.currentTarget.value as ProjectInviteRole)}
          >
            {INVITE_ROLES.map((item) => <option key={item} value={item}>{item}</option>)}
          </PanelSelect>
          <Button className="btn-primary" data-testid="project-invite-submit" type="submit" disabled={action.busy !== null || !email.trim()}>Invite</Button>
        </form>
      )}
      <InlineStatus error={access.data?.memberError ?? null} />
      <SettingsTable
        headers={['Member', 'Role', '']}
        rows={(members ?? []).map((member) => [
          member.email,
          <PanelSelect
            key={`${memberKey(member)}-role`}
            data-testid={`project-member-role-${memberKey(member)}`}
            value={member.role}
            disabled={
              action.busy !== null ||
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
              action.busy !== null ||
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
      <InlineStatus error={access.data?.inviteError ?? null} />
      <SettingsTable
        headers={['Pending invite', 'Role', 'Expires', '']}
        rows={(invites ?? []).map((invite) => [
          invite.email,
          invite.role,
          formatDate(invite.expires_at),
          <Button
            key={invite.id}

            data-testid={`project-invite-revoke-${invite.id}`}
            type="button"
            disabled={action.busy !== null || !canManageProject}
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
