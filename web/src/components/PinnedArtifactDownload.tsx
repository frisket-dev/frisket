import { useState } from 'react';

import {
  ApiError,
  startArtifactPull,
  type DownloadableArtifact,
  type ModelPullDto,
} from '../api/open';
import { formatNumberDisplay } from '../format';
import { ModelPullProgress } from './ModelPullProgress';

export function PinnedArtifactDownload({
  artifact,
  onInstalled,
}: {
  artifact: DownloadableArtifact;
  onInstalled?(): void;
}) {
  const [pull, setPull] = useState<ModelPullDto | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const start = async () => {
    setStarting(true);
    setError(null);
    try {
      const result = await startArtifactPull(artifact.ref);
      setPull(result.pull);
    } catch (caught) {
      if (caught instanceof ApiError && caught.details && typeof caught.details === 'object') {
        const active = (caught.details as { active?: ModelPullDto }).active;
        if (active && typeof active.id === 'number') {
          setPull(active);
          return;
        }
      }
      setError(caught instanceof Error ? caught.message : 'Download failed.');
    } finally {
      setStarting(false);
    }
  };

  if (pull) {
    return (
      <ModelPullProgress
        pull={pull}
        onDone={() => {
          setPull(null);
          onInstalled?.();
        }}
        onFailed={(finished) => {
          setPull(null);
          setError(
            finished.error?.message
              ?? (finished.status === 'cancelled' ? 'Download cancelled.' : 'Download failed.'),
          );
        }}
      />
    );
  }

  const size = artifact.size == null
    ? null
    : formatNumberDisplay(artifact.size, 'filesize');
  return (
    <div className="action-source-block" data-testid="engine-artifact-download">
      <p className="form-hint">
        {artifact.display_name} is a pinned {artifact.license}-licensed model
        {size ? ` (${size})` : ''}. It downloads only after you confirm here.
      </p>
      <button
        type="button"
        className="btn btn-secondary"
        data-testid="engine-artifact-download-button"
        disabled={starting}
        onClick={() => void start()}
      >
        {starting ? 'Starting…' : `Download ${artifact.display_name}`}
      </button>
      {error && <p className="form-error" role="alert">{error}</p>}
    </div>
  );
}
