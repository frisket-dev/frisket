// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import type { ProjectInfo, ProjectRetentionPolicy } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { ProjectDataManagementSettings } from '../../src/settings/ProjectDataManagementSettings';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const projectApi = createProjectApi('alpha');
const getProjectRetention = vi.spyOn(projectApi, 'getProjectRetention');
const updateProjectRetention = vi.spyOn(projectApi, 'updateProjectRetention');
const getProjectSettings = vi.spyOn(projectApi, 'getProjectSettings');
const compactProject = vi.spyOn(projectApi, 'compactProject');
const getProject = vi.spyOn(projectApi, 'getProject');

const { render } = createWorkspaceTestHarness({
  projectId: 'alpha',
  api: { projectApi },
});

const PROJECT: ProjectInfo = {
  id: 'alpha',
  name: 'Alpha Project',
  description: 'Field reporting workspace',
  sensitive: true,
  allowed_services: 'all_services',
  usage_data_sharing: 'off',
  role: 'owner',
};

const RETENTION: ProjectRetentionPolicy = {
  schemaVersion: 'frisket.project_retention_policy.v1',
  default_evidence: 'compactable',
  pin_evidence_by_default: false,
  no_compact: false,
  supported_default_evidence: ['compactable', 'pinned', 'materialized'],
};

beforeAll(() => {
  installPopoverPolyfill();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function stubLoaders() {
  getProject.mockResolvedValue(PROJECT);
  getProjectRetention.mockResolvedValue(RETENTION);
  getProjectSettings.mockResolvedValue({
    media_allow_private_hosts: false,
    media_allow_private_hosts_locked: false,
    retrieval: {
      text_enabled: true,
      pdf_pages_enabled: true,
      image_enabled: false,
      audio_enabled: false,
      transcript_enabled: true,
      prepare_new_files_automatically: false,
      transcribe_new_files_automatically: false,
      video: {
        strategy: 'off',
        duration_seconds: null,
        interval_seconds: null,
        sample_fps: null,
        stride_seconds: null,
      },
    },
  });
}

describe('ProjectDataManagementSettings', () => {
  it('renders export links, updates supporting-records retention, and gates manual compaction on COMPACT', async () => {
    stubLoaders();
    updateProjectRetention.mockResolvedValue({ ...RETENTION, default_evidence: 'pinned' });
    compactProject.mockResolvedValue({
      skipped: false,
      reason: null,
      results_pruned: 2,
      blobs_removed: 1,
      bytes_freed: 256,
      db_bytes_before: 4096,
      db_bytes_after: 3072,
      db_bytes_reclaimed: 1024,
    });

    const user = userEvent.setup();
    render(<ProjectDataManagementSettings project={PROJECT} />);

    expect(await screen.findByTestId('export-project-with-media')).toHaveAttribute(
      'href',
      '/api/projects/alpha/export?mode=bundle&include_media=true&include_traces=false',
    );
    expect(screen.getByTestId('export-project-without-media')).toHaveAttribute(
      'href',
      '/api/projects/alpha/export?mode=bundle&include_media=false&include_traces=false',
    );
    expect(screen.getByTestId('export-project-database')).toHaveAttribute(
      'href',
      '/api/projects/alpha/export?mode=db',
    );
    expect(screen.getByText('Includes project data and stored media files.')).toBeVisible();
    expect(screen.getByText('SQLite database snapshot for backup or inspection.')).toBeVisible();

    const evidenceDefault = await screen.findByTestId('project-evidence-default');
    await user.click(evidenceDefault);
    const evidenceMenu = screen.getByTestId('project-evidence-default-menu');
    expect(evidenceMenu).toHaveTextContent(
      "Manual compaction may remove a run's supporting records; sheet results stay.",
    );
    expect(evidenceMenu).toHaveTextContent(
      "Keep a run's supporting records when this project is manually compacted.",
    );
    expect(evidenceMenu).toHaveTextContent(
      'Keep result-linked supporting records as part of the stored result.',
    );

    await user.click(within(evidenceMenu).getByRole('option', { name: /Pinned/ }));
    await waitFor(() => expect(updateProjectRetention).toHaveBeenCalledWith({ default_evidence: 'pinned' }));
    expect(await screen.findByText('Retention policy saved')).toBeVisible();

    const compactButton = screen.getByTestId('compact-project-button');
    expect(compactButton).toBeDisabled();
    await user.type(screen.getByTestId('compact-confirm-input'), 'COMPACT');
    expect(compactButton).toBeEnabled();
    await user.click(compactButton);

    await waitFor(() => expect(compactProject).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('Compaction reclaimed 1 KB and removed 1 blob.')).toBeVisible();
  });
});
