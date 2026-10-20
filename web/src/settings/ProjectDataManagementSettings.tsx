import { Button } from '../components/Button';
import { FormField } from '../components/FormControls';
import { type FormEvent, useState } from 'react';
import { AlertTriangle, Archive, Database, Download } from 'lucide-react';
import {
  type ProjectInfo,
  type ProjectNetworkPolicy,
  type ProjectRetentionPolicy,
  type ProjectSettings,
  type ProjectSettingsPatch,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { canOwnProject, canReviewProject } from '../api/projectRole';
import { PanelSelect, type PanelSelectOption } from '../components/PanelSelect';
import { ConfirmTypeInput, PanelNotice } from '../components/PanelPrimitives';
import { ProjectRetrievalSettings } from './ProjectRetrievalSettings';
import { sendProductTelemetry } from '../telemetry/productTelemetry';
import { useLoader, errMsg } from './settingsSupport';
import { InlineStatus } from './SettingsPrimitives';
import { formatBytes } from '../format';

const FALLBACK_EVIDENCE_RETENTION_VALUES = ['compactable', 'pinned', 'materialized'] as const;

const EVIDENCE_RETENTION_LABELS: Record<string, string> = {
  compactable: 'Compactable',
  pinned: 'Pinned',
  materialized: 'Materialized',
};

const EVIDENCE_RETENTION_DESCRIPTIONS: Record<string, string> = {
  compactable: 'Save space. Manual compaction may remove supporting run evidence; sheet results stay.',
  pinned: 'Keep supporting run evidence when this project is manually compacted.',
  materialized: 'Keep output-linked evidence as part of the stored result.',
};

function plural(value: unknown, singular: string): string {
  return value === 1 ? singular : `${singular}s`;
}

function supportedEvidenceOptions(policy: ProjectRetentionPolicy | null): string[] {
  const raw = Array.isArray(policy?.supported_default_evidence)
    ? policy.supported_default_evidence
    : [...FALLBACK_EVIDENCE_RETENTION_VALUES];
  const supported = raw.filter((value): value is string => (
    typeof value === 'string' && value in EVIDENCE_RETENTION_LABELS
  ));
  return supported.length ? supported : [...FALLBACK_EVIDENCE_RETENTION_VALUES];
}

export function ProjectDataManagementSettings({ project }: { project?: ProjectInfo | null }) {
  const { projectApi: api, chromePreferences: { projectId } } = useWorkspaceStores();
  const retention = useLoader<ProjectRetentionPolicy>(() => api.getProjectRetention());
  const settings = useLoader<ProjectSettings>(() => api.getProjectSettings());
  const network = useLoader<ProjectNetworkPolicy>(() => api.getProjectNetworkPolicy());
  const [notice, setNotice] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [compactConfirm, setCompactConfirm] = useState('');
  const [compacting, setCompacting] = useState(false);
  const canMutate = canOwnProject(project);
  const canExport = canReviewProject(project);
  const policy = retention.data;
  const projectSettings = settings.data;
  const evidenceOptions = supportedEvidenceOptions(policy);
  const evidenceSelectOptions: PanelSelectOption[] = evidenceOptions.map((value) => ({
    value,
    label: EVIDENCE_RETENTION_LABELS[value] ?? value,
    description: EVIDENCE_RETENTION_DESCRIPTIONS[value]
      ?? 'Use the retention behavior provided by this project.',
  }));
  const patch = async (input: Partial<ProjectRetentionPolicy>) => {
    if (!canMutate) return;
    setMutationError(null);
    try {
      retention.setData(await api.updateProjectRetention(input));
      setNotice('Retention policy saved');
    } catch (caught) {
      setNotice(null);
      setMutationError(errMsg(caught));
    }
  };
  const patchSettings = async (input: ProjectSettingsPatch) => {
    if (!canMutate) return;
    setMutationError(null);
    try {
      settings.setData(await api.updateProjectSettings(input));
      setNotice('Project settings saved');
    } catch (caught) {
      setNotice(null);
      setMutationError(errMsg(caught));
    }
  };
  const patchNetwork = async (mode: string) => {
    if (!canMutate) return;
    setMutationError(null);
    try {
      network.setData(await api.updateProjectNetworkPolicy({ mode }));
      setNotice('Network setting saved');
    } catch (caught) {
      setNotice(null);
      setMutationError(errMsg(caught));
    }
  };
  const compact = async (event: FormEvent) => {
    event.preventDefault();
    if (!canMutate || policy?.no_compact || compactConfirm !== 'COMPACT') return;
    setCompacting(true);
    setMutationError(null);
    try {
      const summary = await api.compactProject();
      setCompactConfirm('');
      if (summary.skipped) {
        setNotice('Compaction skipped because compaction is disabled for this project.');
      } else {
        const reclaimed = formatBytes(summary.db_bytes_reclaimed ?? summary.bytes_freed);
        const blobsRemoved = typeof summary.blobs_removed === 'number' ? summary.blobs_removed : 0;
        setNotice(`Compaction reclaimed ${reclaimed} and removed ${blobsRemoved} ${plural(blobsRemoved, 'blob')}.`);
      }
    } catch (caught) {
      setNotice(null);
      setMutationError(errMsg(caught));
    } finally {
      setCompacting(false);
    }
  };
  return (
    <div className="content-stack" data-testid="project-data-management-settings">
      <InlineStatus
        error={retention.error ?? settings.error ?? network.error ?? mutationError}
        loading={retention.loading || settings.loading || network.loading}
        notice={notice}
      />
      {!canMutate && (
        <PanelNotice>
          Your project role can view retention settings but cannot change retention or compact the project.
        </PanelNotice>
      )}
      <section className="content-stack" data-testid="project-export-section">
        <div className="settings-section-subhead">
          <h2>Exports</h2>
          <p>Download portable copies of this project. Exports do not change project data.</p>
        </div>
        {canExport ? (
          <div className="settings-action-list">
            <div className="settings-action-row">
              <div>
                <strong>Export with media</strong>
                <p>Includes project data and stored media files.</p>
              </div>
              <a className="btn" data-testid="export-project-with-media" href={api.projectExportUrl(true)} download onClick={() => sendProductTelemetry({ type: 'Export.requested', properties: { exportKind: 'project_bundle' } }, projectId)}>
                <Download size={14} /> Download
              </a>
            </div>
            <div className="settings-action-row">
              <div>
                <strong>Export without media</strong>
                <p>Includes project structure and records, excluding blob media payloads.</p>
              </div>
              <a className="btn" data-testid="export-project-without-media" href={api.projectExportUrl(false)} download onClick={() => sendProductTelemetry({ type: 'Export.requested', properties: { exportKind: 'project_bundle_without_media' } }, projectId)}>
                <Archive size={14} /> Download
              </a>
            </div>
            <div className="settings-action-row">
              <div>
                <strong>Database snapshot</strong>
                <p>SQLite database snapshot for backup or inspection.</p>
              </div>
              <a className="btn" data-testid="export-project-database" href={api.projectExportUrl({ mode: 'db' })} download onClick={() => sendProductTelemetry({ type: 'Export.requested', properties: { exportKind: 'project_database' } }, projectId)}>
                <Database size={14} /> Download
              </a>
            </div>
          </div>
        ) : (
          <PanelNotice>
            A viewer can read this project but cannot take a full copy of it. Ask
            someone with reviewer access or higher to export it.
          </PanelNotice>
        )}
      </section>
      {policy && (
        <section className="content-stack">
          <div className="settings-section-subhead">
            <h2>Retention and evidence</h2>
            <p>
              Choose how much supporting evidence Frisket keeps for future review.
              This affects manual compaction, not the values already visible in your sheets.
            </p>
          </div>
          <div className="settings-grid-two">
          <FormField label="Evidence default" >

            <PanelSelect
              testId="project-evidence-default"
              ariaLabel="Evidence default"
              value={policy.default_evidence}
              disabled={!canMutate}
              options={evidenceSelectOptions}
              onValueChange={(value) => void patch({ default_evidence: value })}
            />
            <small className="form-hint">
              This is the usual default. Frisket may keep particular evidence when an action needs it
              to preserve a result or its lineage.
            </small>
          </FormField>
          <label className="settings-check">
            <input
              type="checkbox"
              checked={policy.no_compact}
              disabled={!canMutate}
              onChange={(event) => void patch({ no_compact: event.currentTarget.checked })}
            />
            <span>Disable manual compaction</span>
          </label>
          </div>
        </section>
      )}
      {network.data && (
        <section className="content-stack" data-testid="project-network-section">
          <div className="settings-section-subhead">
            <h2>Network</h2>
            <p>
              Control whether project actions may contact outside services such as
              cloud AI providers, web search, geocoding, and public URLs. Turn this
              off for offline or especially sensitive work. Local tools and your
              configured local model servers still work.
            </p>
          </div>
          <FormField label="Network access" >

            <PanelSelect
              data-testid="project-network-mode"
              value={network.data.mode}
              disabled={!canMutate}
              onChange={(event) => void patchNetwork(event.currentTarget.value)}
            >
              <option value="inherit">
                {`Inherit (currently ${network.data.org_default ?? 'on'})`}
              </option>
              <option value="on">On</option>
              <option value="off">Off</option>
            </PanelSelect>
            <small className="form-hint">
              {`Currently ${network.data.effective}. “Inherit” follows your organization’s
              setting. Plugins, notifications, and scheduled feed checks have their own
              network behavior.`}
            </small>
          </FormField>
        </section>
      )}
      {projectSettings?.retrieval && (
        <ProjectRetrievalSettings
          settings={projectSettings.retrieval}
          canMutate={canMutate}
          onPatch={patchSettings}
        />
      )}
      {projectSettings && (
        <section className="content-stack" data-testid="project-media-downloads-section">
          <div className="settings-section-subhead">
            <h2>Media downloads</h2>
            <p>
              Decide whether media URLs may point to servers on the same private network
              as Frisket. Keep private addresses off when URLs come from imported,
              scraped, or otherwise untrusted data.
            </p>
          </div>
          {projectSettings.media_allow_private_hosts_locked ? (
            <small
              className="form-hint"
              data-testid="project-media-private-hosts-locked"
            >
              Downloads from private and loopback addresses are switched off and locked
              by the server (FRISKET_MEDIA_PRIVATE_HOSTS=deny-locked). There is no
              per-project override.
            </small>
          ) : (
            <>
              <label className="settings-check">
                <input
                  type="checkbox"
                  data-testid="project-media-allow-private-hosts"
                  checked={projectSettings.media_allow_private_hosts}
                  disabled={!canMutate}
                  onChange={(event) => void patchSettings({ media_allow_private_hosts: event.currentTarget.checked })}
                />
                <span>Allow downloads from private and loopback addresses</span>
              </label>
              <small className="form-hint">
                Turn this on only when you intentionally download from a server on your
                own network. Link-local and cloud metadata addresses remain blocked.
                Your administrator&apos;s network controls are always the final boundary.
              </small>
            </>
          )}
        </section>
      )}
      <form className="settings-danger-zone" data-testid="project-compact-danger-zone" onSubmit={compact}>
        <div className="settings-danger-header">
          <AlertTriangle size={16} />
          <div>
            <h2>Manual compaction</h2>
            <p>
              Free disk space by permanently deleting old run data and unused files
              that are no longer needed by the current project. Export a backup first
              if you may need that history later.
            </p>
          </div>
        </div>
        {policy?.no_compact && (
          <PanelNotice>
            Manual compaction is disabled by this project's retention policy.
          </PanelNotice>
        )}
        <label>
          <span>Type COMPACT to compact this project</span>
          <ConfirmTypeInput
            data-testid="compact-confirm-input"
            value={compactConfirm}
            disabled={!canMutate || compacting || policy?.no_compact}
            onChange={setCompactConfirm}
          />
        </label>
        <Button
          className="btn-reject settings-danger-button"
          data-testid="compact-project-button"
          type="submit"
          disabled={!canMutate || compacting || policy?.no_compact || compactConfirm !== 'COMPACT'}
        >
          <Archive size={14} /> Compact
        </Button>
      </form>
    </div>
  );
}
