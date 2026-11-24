import { useState } from 'react';
import type { AllowedServices, ProjectInfo, UsageDataSharing } from '../api/types';
import { getRuntimeConfig } from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { ALLOWED_SERVICES_LABELS, isLooserAllowedServices } from '../allowedServices';
import { AllowedServicesChoice } from '../components/AllowedServicesChoice';
import { FormField } from '../components/FormControls';
import { ConfirmDialog } from '../components/Modal';
import { PanelSelect, type PanelSelectOption } from '../components/PanelSelect';
import { errorMessage } from '../hooks/errorMessage';
import { useLoader } from '../hooks/useLoader';
import { navigate } from '../routes';
import { useShellIdentity } from '../shellIdentity';
import { workspaceSharesUsageData } from '../telemetry/productTelemetry';
import { InlineStatus } from './SettingsPrimitives';

const USAGE_DATA_VALUES: readonly UsageDataSharing[] = ['inherit', 'off', 'on'];

function usageDataOptions(workspaceShares: boolean): PanelSelectOption[] {
  return [
    {
      value: 'inherit',
      label: `Follow the workspace setting (currently ${workspaceShares ? 'shares' : "doesn't share"})`,
    },
    { value: 'off', label: "Don't share usage data from this project" },
    { value: 'on', label: 'Share usage data from this project' },
  ];
}

function isUsageDataSharing(value: string): value is UsageDataSharing {
  return USAGE_DATA_VALUES.some((option) => option === value);
}

/** A project's Allowed services and usage-data settings. A looser Allowed
 *  services value asks first and sends the confirmation the server requires;
 *  a stricter one applies at once. */
export function ProjectServiceSettings({
  canMutate,
}: {
  canMutate: boolean;
}) {
  const { projectApi: api } = useWorkspaceStores();
  const { identityMode } = useShellIdentity();
  const details = useLoader<ProjectInfo>(() => api.getProject(), []);
  const runtime = useLoader(() => getRuntimeConfig(), []);
  const [pending, setPending] = useState<AllowedServices | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const current = details.data?.allowed_services ?? null;
  const usage = details.data?.usage_data_sharing ?? null;
  const workspaceShares = workspaceSharesUsageData();

  const save = async (write: () => Promise<ProjectInfo>, done: string) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      details.setData(await write());
      setNotice(done);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const choose = (next: AllowedServices) => {
    if (current === null || next === current) return;
    if (isLooserAllowedServices(current, next)) {
      setPending(next);
      return;
    }
    void save(() => api.updateCurrentProjectAllowedServices(next, null), 'Allowed services saved');
  };

  const confirmLooser = () => {
    if (pending === null) return;
    const next = pending;
    setPending(null);
    void save(() => api.updateCurrentProjectAllowedServices(next, next), 'Allowed services saved');
  };

  return (
    <>
      <section className="content-stack" data-testid="project-allowed-services-section">
        <div className="settings-section-subhead">
          <h2>Allowed services</h2>
          <p>Choose which services Frisket can use when working with this project&apos;s data.</p>
        </div>
        <InlineStatus error={details.error ?? error} loading={details.loading} notice={notice} />
        {current !== null && (
          <AllowedServicesChoice
            value={current}
            offered={runtime.data?.allowed_services_offered ?? [current]}
            onChange={choose}
            disabled={!canMutate || busy}
            legendVisible={false}
            onManageEndpoints={() => navigate({
              kind: 'settings',
              scope: identityMode ? 'organization' : 'personal',
              section: 'approved-endpoints',
            })}
          />
        )}
      </section>
      {usage !== null && (
        <section className="content-stack" data-testid="project-usage-data-section">
          <div className="settings-section-subhead">
            <h2>Usage data</h2>
            <p>Usage data is the product telemetry set under Personal, Privacy. It does not include your data.</p>
          </div>
          <FormField label="Usage data from this project">
            <PanelSelect
              testId="project-usage-data-sharing"
              ariaLabel="Usage data from this project"
              value={usage}
              disabled={!canMutate || busy}
              options={usageDataOptions(workspaceShares)}
              onValueChange={(value) => {
                if (isUsageDataSharing(value) && value !== usage) {
                  void save(() => api.updateCurrentProjectUsageDataSharing(value), 'Usage data setting saved');
                }
              }}
            />
          </FormField>
        </section>
      )}
      {pending !== null && current !== null && (
        <ConfirmDialog
          title="Allow more services?"
          confirmLabel={`Change to ${ALLOWED_SERVICES_LABELS[pending]}`}
          testId="allowed-services-confirm"
          confirmTestId="allowed-services-confirm-button"
          cancelTestId="allowed-services-confirm-cancel"
          onConfirm={confirmLooser}
          onCancel={() => setPending(null)}
        >
          <p className="modal-body-text">
            {`Change Allowed services from ${ALLOWED_SERVICES_LABELS[current]} to ${ALLOWED_SERVICES_LABELS[pending]}? `}
            Frisket will be able to use more services with this project&apos;s data. The change is
            recorded in the project&apos;s history; work already run is unaffected.
          </p>
        </ConfirmDialog>
      )}
    </>
  );
}
