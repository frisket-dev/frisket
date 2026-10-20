// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi, type Mock } from 'vitest';
import { ProjectSecretsSettings } from '../../src/settings/ProjectIntegrationSettings';
import { OrganizationSecretsSettings } from '../../src/settings/OrganizationSettings';
import { getRuntimeConfig, listOrgEnvVars, setOrgEnvVar } from '../../src/api/open';
import { useSecretEntryDraft } from '../../src/settings/useSecretEntryDraft';

const projectApi = vi.hoisted(() => ({
  getProjectSecrets: vi.fn(),
  setProjectSecret: vi.fn(),
}));
vi.mock('../../src/bind/useWorkspaceStores', () => ({ useWorkspaceStores: () => ({ projectApi }) }));
vi.mock('../../src/api/open', async (original) => ({
  ...await original<typeof import('../../src/api/open')>(),
  getRuntimeConfig: vi.fn(), listOrgEnvVars: vi.fn(), setOrgEnvVar: vi.fn(),
}));

const emptySecrets = { schemaVersion: 'frisket.project_secrets.v1', secrets: [], conflicts: [] };
const owner = { id: 'one', name: 'One', role: 'owner' as const };
const cases = [
  { scope: 'project', name: 'Project secret name', value: 'Project secret value',
    render: () => render(<ProjectSecretsSettings project={owner} />), save: projectApi.setProjectSecret },
  { scope: 'organization', name: 'Secret name', value: 'Secret value',
    render: () => render(<OrganizationSecretsSettings />), save: vi.mocked(setOrgEnvVar) as Mock },
];

beforeEach(() => {
  projectApi.getProjectSecrets.mockResolvedValue(emptySecrets);
  vi.mocked(getRuntimeConfig).mockResolvedValue({ plugins_available: false } as Awaited<ReturnType<typeof getRuntimeConfig>>);
  vi.mocked(listOrgEnvVars).mockResolvedValue([]);
});
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it.each(cases)('$scope completion preserves a newer typed draft and refuses duplicate submissions', async (test) => {
  let complete!: (value: unknown) => void;
  test.save.mockReturnValue(new Promise((resolve) => { complete = resolve; }));
  test.render();
  const name = await screen.findByLabelText(test.name);
  const value = screen.getByLabelText(test.value);
  fireEvent.change(name, { target: { value: 'FIRST' } });
  fireEvent.change(value, { target: { value: 'first-value' } });
  const form = name.closest('form')!;
  fireEvent.submit(form);
  fireEvent.submit(form);
  fireEvent.change(name, { target: { value: 'SECOND' } });
  fireEvent.change(value, { target: { value: 'second-value' } });
  await act(async () => { complete(emptySecrets); });
  expect(name).toHaveValue('SECOND');
  expect(value).toHaveValue('second-value');
  expect(test.save).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
  expect(test.save).toHaveBeenCalledWith('FIRST', 'first-value');
});

it.each(cases)('$scope failure retains the draft and success clears only its submitted revision', async (test) => {
  test.save.mockRejectedValueOnce(new Error('Could not save secret')).mockResolvedValueOnce(emptySecrets);
  test.render();
  const name = await screen.findByLabelText(test.name);
  const value = screen.getByLabelText(test.value);
  fireEvent.change(name, { target: { value: 'TOKEN' } });
  fireEvent.change(value, { target: { value: 'value' } });
  fireEvent.submit(name.closest('form')!);
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not save secret');
  expect(name).toHaveValue('TOKEN');
  expect(value).toHaveValue('value');
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled());
  fireEvent.submit(name.closest('form')!);
  await waitFor(() => expect(name).toHaveValue(''));
  expect(value).toHaveValue('');
});

it('keeps project secret mutation permission checks at the caller', async () => {
  const { rerender } = render(<ProjectSecretsSettings project={owner} />);
  const name = await screen.findByLabelText('Project secret name');
  fireEvent.change(name, { target: { value: 'TOKEN' } });
  fireEvent.change(screen.getByLabelText('Project secret value'), { target: { value: 'value' } });
  rerender(<ProjectSecretsSettings project={{ ...owner, role: 'viewer' }} />);
  expect(name).toBeDisabled();
  fireEvent.submit(name.closest('form')!);
  expect(projectApi.setProjectSecret).not.toHaveBeenCalled();
});

it('retires old scope submissions without releasing another scope pending save', () => {
  const { result, rerender, unmount } = renderHook(({ scope }) => useSecretEntryDraft(scope), {
    initialProps: { scope: 'one' },
  });
  act(() => { result.current.setName('FIRST'); result.current.setValue('value'); });
  let first!: NonNullable<ReturnType<typeof result.current.begin>>;
  act(() => { first = result.current.begin()!; });
  const completeFirst = result.current.complete;
  rerender({ scope: 'two' });
  rerender({ scope: 'one' });
  expect(result.current.name).toBe('');
  expect(result.current.saving).toBe(false);
  rerender({ scope: 'two' });
  act(() => { result.current.setName('SECOND'); result.current.setValue('value'); });
  let second!: NonNullable<ReturnType<typeof result.current.begin>>;
  act(() => { second = result.current.begin()!; });
  act(() => { expect(completeFirst(first)).toBe(false); });
  expect(result.current.saving).toBe(true);
  expect(result.current.name).toBe('SECOND');
  act(() => { result.current.complete(second); });
  expect(result.current.name).toBe('');
  unmount();
});

it('preserves the project deep-link name and initial focus on its value', async () => {
  window.history.replaceState(null, '', '/settings?secret=FROM_LINK');
  render(<ProjectSecretsSettings project={owner} />);
  expect(await screen.findByLabelText('Project secret name')).toHaveValue('FROM_LINK');
  expect(screen.getByLabelText('Project secret value')).toHaveFocus();
  window.history.replaceState(null, '', '/');
});
