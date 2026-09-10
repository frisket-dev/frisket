// @vitest-environment jsdom
//
// Project-scoped stdio MCP configuration. This is deliberately a component
// contract: server management is a Solo Project Settings surface and must not
// leak into team/workspace settings or the ordinary action launcher.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ComponentProps } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { McpServersSection } from '../../src/settings/SettingsSections';
import { groupedSettingsNavItems } from '../../src/settings/settingsNav';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

type Server = {
  id: string;
  name: string;
  command: string;
  args: string[];
  cwd?: string | null;
  env: Record<string, { project_secret: string } | { value: string }>;
  enabled: boolean;
  last_discovered_tool_count?: number;
  last_test?: {
    status: 'passed' | 'failed';
    tested_at: string;
    detail?: string;
  } | null;
};

const CRM: Server = {
  id: 'local-crm',
  name: 'Local CRM',
  command: 'uvx',
  args: ['company-crm-mcp'],
  cwd: null,
  env: { CRM_TOKEN: { project_secret: 'CRM_TOKEN' } },
  enabled: true,
  last_discovered_tool_count: 8,
  last_test: { status: 'passed', tested_at: '2026-09-02T11:52:00Z' },
};

const NEVER_TESTED: Server = {
  ...CRM,
  id: 'filesystem',
  name: 'Filesystem',
  command: 'npx',
  args: ['-y', '@modelcontextprotocol/server-filesystem', '/tmp'],
  env: {},
  last_discovered_tool_count: 5,
  last_test: null,
};

const FAILED: Server = {
  ...CRM,
  id: 'stale-server',
  name: 'Stale server',
  enabled: false,
  last_discovered_tool_count: 12,
  last_test: {
    status: 'failed',
    tested_at: '2026-09-02T11:51:00Z',
    detail: 'Command exited before initialize.',
  },
};

const LITERAL_AND_SPACED_ARG: Server = {
  ...CRM,
  id: 'literal-and-spaced-arg',
  name: 'Literal and spaced arg',
  args: ['--query', 'customer name with spaces'],
  env: {
    // This non-secret setting may be sent as a literal.  In contrast, the
    // credential remains a reference: no secret plaintext arrives in a GET
    // response or is rendered back into this form.
    LOG_LEVEL: { value: 'debug' },
    CRM_TOKEN: { project_secret: 'CRM_TOKEN' },
  },
};

function renderSection(overrides: Partial<ComponentProps<typeof McpServersSection>> = {}) {
  const onCreate = vi.fn();
  const onUpdate = vi.fn();
  const onRemove = vi.fn();
  const onTest = vi.fn().mockResolvedValue({
    status: 'passed',
    tested_at: '2026-09-02T12:00:00Z',
    discovered_tool_count: 8,
  });

  return {
    onCreate,
    onUpdate,
    onRemove,
    onTest,
    ...render(
      <McpServersSection
        servers={[CRM, NEVER_TESTED, FAILED]}
        projectSecrets={['CRM_TOKEN', 'OTHER_SECRET']}
        onCreate={onCreate}
        onUpdate={onUpdate}
        onRemove={onRemove}
        onTest={onTest}
        {...overrides}
      />,
    ),
  };
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('McpServersSection — Solo Project Settings', () => {
  it('is an enabled local Project Settings destination and absent from hosted navigation', () => {
    const local = groupedSettingsNavItems({ projectId: 'project-7', identityMode: false })
      .flatMap((group) => group.items)
      .find((item) => item.definition.id === 'project.mcp-servers');
    const hosted = groupedSettingsNavItems({ projectId: 'project-7', identityMode: true })
      .flatMap((group) => group.items)
      .find((item) => item.definition.id === 'project.mcp-servers');

    expect(local).toMatchObject({
      disabled: false,
      path: '/p/project-7/settings/project/mcp-servers',
      definition: {
        component: 'project.mcpServers',
        visibility: 'local-only',
      },
    });
    expect(hosted).toBeUndefined();
  });

  it('lists configured servers with honest enabled and test status', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-02T12:00:00Z'));
    renderSection();

    expect(screen.getByRole('heading', { name: /mcp servers/i })).toBeInTheDocument();
    expect(screen.getByTestId('mcp-server-local-crm')).toHaveTextContent(
      'Enabled · last tested 8 minutes ago',
    );
    expect(screen.getByTestId('mcp-server-local-crm')).toHaveTextContent('8 tools last discovered');
    expect(screen.getByTestId('mcp-server-filesystem')).toHaveTextContent('Enabled · never tested');
    expect(screen.getByTestId('mcp-server-stale-server')).toHaveTextContent('Disabled · last test failed');
    expect(screen.getByTestId('mcp-server-stale-server')).toHaveTextContent(
      'Command exited before initialize.',
    );
  });

  it('adds a stdio server manually without a shell command string', async () => {
    const { onCreate } = renderSection({ servers: [] });
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /add mcp server/i }));
    await user.type(screen.getByLabelText(/^name$/i), 'Document tools');
    await user.type(screen.getByLabelText(/^command$/i), 'uvx');
    await user.type(screen.getByLabelText(/^arguments$/i), 'document-tools-mcp');
    await user.click(screen.getByRole('button', { name: /add environment variable/i }));
    await user.type(screen.getByLabelText(/environment variable name/i), 'CRM_TOKEN');
    await user.selectOptions(screen.getByLabelText(/^crm token$/i), 'CRM_TOKEN');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    expect(onCreate).toHaveBeenCalledWith({
      name: 'Document tools',
      command: 'uvx',
      args: ['document-tools-mcp'],
      cwd: null,
      env: { CRM_TOKEN: { project_secret: 'CRM_TOKEN' } },
      enabled: true,
    });
  });

  it('imports a standard mcpServers stanza and requires explicit secret mapping', async () => {
    const { onCreate } = renderSection({ servers: [] });
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /import mcp json/i }));
    fireEvent.change(screen.getByLabelText(/mcp server json/i), { target: { value: JSON.stringify({
      mcpServers: {
        documents: {
          command: 'uvx',
          args: ['document-tools-mcp', '/path with spaces'],
          env: { DOCUMENT_API_TOKEN: '${DOCUMENT_API_TOKEN}' },
        },
      },
    }) } });
    await user.click(screen.getByRole('button', { name: /^import$/i }));

    expect(screen.getByLabelText(/^document api token$/i)).toBeInTheDocument();
    expect(screen.getByText(/map imported environment values/i)).toBeInTheDocument();
    expect(onCreate).not.toHaveBeenCalled();

    await user.selectOptions(screen.getByLabelText(/^document api token$/i), 'CRM_TOKEN');
    await user.click(screen.getByRole('button', { name: /^save$/i }));
    expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
      name: 'documents',
      command: 'uvx',
      args: ['document-tools-mcp', '/path with spaces'],
      env: { DOCUMENT_API_TOKEN: { project_secret: 'CRM_TOKEN' } },
    }));
  });

  it('refuses to silently drop extra servers from an imported config', async () => {
    const { onCreate } = renderSection({ servers: [] });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /import mcp json/i }));
    fireEvent.change(screen.getByLabelText(/mcp server json/i), { target: { value: JSON.stringify({
      mcpServers: {
        first: { command: 'uvx', args: ['first-mcp'] },
        second: { command: 'uvx', args: ['second-mcp'] },
      },
    }) } });
    await user.click(screen.getByRole('button', { name: /^import$/i }));
    expect(screen.getByRole('alert')).toHaveTextContent(/one mcp server at a time/i);
    expect(onCreate).not.toHaveBeenCalled();
  });

  it('edits, tests, enables/disables, and removes a server through explicit actions', async () => {
    const { onUpdate, onRemove, onTest } = renderSection();
    const user = userEvent.setup();
    const crm = screen.getByTestId('mcp-server-local-crm');

    await user.click(screen.getByRole('button', { name: /edit local crm/i }));
    const name = screen.getByLabelText(/^name$/i);
    await user.clear(name);
    await user.type(name, 'CRM tools');
    await user.click(screen.getByRole('button', { name: /^save$/i }));
    expect(onUpdate).toHaveBeenCalledWith('local-crm', expect.objectContaining({ name: 'CRM tools' }));

    await user.click(screen.getByRole('button', { name: /test local crm/i }));
    expect(onTest).toHaveBeenCalledWith('local-crm');
    expect(await screen.findByTestId('mcp-server-test-result-local-crm')).toHaveTextContent(/passed/i);

    await user.click(screen.getByRole('button', { name: /disable local crm/i }));
    expect(onUpdate).toHaveBeenCalledWith('local-crm', { enabled: false });
    await user.click(screen.getByRole('button', { name: /remove local crm/i }));
    expect(onRemove).toHaveBeenCalledWith('local-crm');

    expect(crm).toBeInTheDocument();
  });

  it('does not silently enable a disabled server when editing it', async () => {
    const { onUpdate } = renderSection({ servers: [FAILED] });
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /edit stale server/i }));
    const name = screen.getByLabelText(/^name$/i);
    await user.clear(name);
    await user.type(name, 'Still disabled');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    expect(onUpdate).toHaveBeenCalledWith(
      'stale-server',
      expect.objectContaining({ name: 'Still disabled', enabled: false }),
    );
  });

  it('round-trips argv entries containing spaces and preserves literal values without exposing secret plaintext', async () => {
    const { onUpdate } = renderSection({ servers: [LITERAL_AND_SPACED_ARG] });
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: /edit literal and spaced arg/i }));

    // The form distinguishes the safe literal setting from a Project Secret
    // reference.  The latter is identified only by its secret name, never its
    // plaintext value.
    expect(screen.getByLabelText(/^log level$/i)).toHaveValue('debug');
    expect(screen.getByLabelText(/^crm token$/i)).toHaveValue('CRM_TOKEN');
    expect(screen.queryByDisplayValue(/super-secret/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /^save$/i }));
    expect(onUpdate).toHaveBeenCalledWith('literal-and-spaced-arg', expect.objectContaining({
      args: ['--query', 'customer name with spaces'],
      env: {
        LOG_LEVEL: { value: 'debug' },
        CRM_TOKEN: { project_secret: 'CRM_TOKEN' },
      },
    }));
  });
});
