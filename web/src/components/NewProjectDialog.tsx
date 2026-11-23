import './NewProjectDialog.css';
import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import type { AllowedServices, UsageDataSharing } from '../api/types';
import type { CreateProjectInput } from '../api/httpContractRoutes/projects';
import { restrictedServicesLabel } from '../allowedServices';
import { AllowedServicesChoice } from './AllowedServicesChoice';
import { Button } from './Button';
import { TextInput } from './FormControls';
import { Modal } from './Modal';
import { ProjectCardFace } from './ProjectCard';
import { newCoverSeed, seedCover } from './projectCover';
import { WindowCloseButton } from './WindowCloseButton';

const UNTITLED_PROJECT_NAME = 'Untitled project';

/** The usage-data value a create request carries: the workspace-wide setting
 *  unless the checkbox moved away from where that setting started it. */
function usageDataSharingFor(dontShare: boolean, workspaceSharesUsage: boolean): UsageDataSharing {
  if (dontShare === !workspaceSharesUsage) return 'inherit';
  return dontShare ? 'off' : 'on';
}

export function NewProjectDialog({
  offered,
  workspaceSharesUsage,
  busy,
  error,
  onCreate,
  onClose,
  onManageEndpoints,
}: {
  offered: readonly AllowedServices[];
  workspaceSharesUsage: boolean;
  busy: boolean;
  error: string | null;
  onCreate(input: CreateProjectInput): void;
  onClose(): void;
  onManageEndpoints(): void;
}) {
  const [name, setName] = useState('');
  const [allowedServices, setAllowedServices] = useState<AllowedServices>('all_services');
  const [dontShare, setDontShare] = useState(!workspaceSharesUsage);
  const [coverSeed, setCoverSeed] = useState(() => newCoverSeed());
  // Captured during the first render, before the dialog moves focus into itself.
  const [trigger] = useState(() => document.activeElement);
  useEffect(() => () => {
    if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus();
  }, [trigger]);

  const trimmed = name.trim();
  const create = () => {
    if (busy) return;
    onCreate({
      name: trimmed,
      allowed_services: allowedServices,
      usage_data_sharing: usageDataSharingFor(dontShare, workspaceSharesUsage),
      cover_seed: coverSeed,
    });
  };

  return (
    <Modal
      title="New project"
      hideHeader
      onClose={onClose}
      busy={busy}
      className="new-project-dialog"
      testId="new-project-dialog"
    >
      <div className="new-project-layout">
        <div className="new-project-preview">
          <div className="home-card new-project-preview-card" data-testid="new-project-preview" aria-hidden>
            <div className="home-card-open">
              <ProjectCardFace
                cover={seedCover(coverSeed)}
                title={trimmed || UNTITLED_PROJECT_NAME}
                titleTestId="new-project-preview-title"
                placeholderTitle={!trimmed}
                meta={<span className="home-card-meta">0 sheets · new</span>}
                servicesLabel={restrictedServicesLabel(allowedServices)}
                servicesTestId="new-project-preview-services"
              />
            </div>
          </div>
          <Button
            type="button"
            variant="link"
            className="new-project-different-cover"
            data-testid="new-project-different-cover"
            disabled={busy}
            onClick={() => setCoverSeed((current) => newCoverSeed(current))}
          >
            <RefreshCw size={12} aria-hidden /> Different cover
          </Button>
        </div>
        <form
          className="new-project-form"
          onSubmit={(event) => {
            event.preventDefault();
            create();
          }}
        >
          <div className="new-project-head">
            <h2 className="new-project-title">New project</h2>
            <WindowCloseButton label="Close" disabled={busy} onClick={onClose} />
          </div>
          <div className="new-project-fields">
            <label className="new-project-name">
              <span className="new-project-label">Name</span>
              <TextInput
                data-modal-autofocus
                data-testid="new-project-name"
                placeholder={UNTITLED_PROJECT_NAME}
                value={name}
                onChange={(event) => setName(event.currentTarget.value)}
              />
            </label>
            <AllowedServicesChoice
              value={allowedServices}
              offered={offered}
              onChange={setAllowedServices}
              disabled={busy}
              onManageEndpoints={onManageEndpoints}
            />
            <label className="new-project-usage">
              <input
                type="checkbox"
                data-testid="new-project-dont-share-usage"
                checked={dontShare}
                disabled={busy}
                onChange={(event) => setDontShare(event.currentTarget.checked)}
              />
              <span>Don&apos;t share usage data from this project</span>
            </label>
            {error && <div className="picker-error" role="alert" data-testid="new-project-error">{error}</div>}
          </div>
          <div className="new-project-footer">
            <Button type="button" disabled={busy} onClick={onClose} data-testid="new-project-cancel">Cancel</Button>
            <Button type="submit" className="btn-primary" disabled={busy} data-testid="create-project">
              Create project
            </Button>
          </div>
        </form>
      </div>
    </Modal>
  );
}
