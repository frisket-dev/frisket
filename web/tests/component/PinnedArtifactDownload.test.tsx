// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { startArtifactPull } = vi.hoisted(() => ({ startArtifactPull: vi.fn() }));

vi.mock('../../src/api/open', () => ({
  ApiError: class ApiError extends Error {},
  startArtifactPull,
}));

vi.mock('../../src/components/ModelPullProgress', () => ({
  ModelPullProgress: ({ onDone }: { onDone?(): void }) => (
    <button type="button" data-testid="fake-pull-complete" onClick={onDone}>
      Complete download
    </button>
  ),
}));

import { PinnedArtifactDownload } from '../../src/components/PinnedArtifactDownload';

describe('PinnedArtifactDownload', () => {
  beforeEach(() => {
    startArtifactPull.mockReset();
  });

  it('does not download until explicit confirmation and reports completion', async () => {
    const onInstalled = vi.fn();
    startArtifactPull.mockResolvedValue({
      deduplicated: false,
      pull: { id: 17, status: 'pending', model: 'spacy:en_core_web_sm@3.8.0' },
    });
    const user = userEvent.setup();
    render(
      <PinnedArtifactDownload
        artifact={{
          ref: 'spacy:en_core_web_sm@3.8.0',
          display_name: 'spaCy English pipeline',
          revision: '3.8.0',
          size: 12_806_118,
          license: 'MIT',
        }}
        onInstalled={onInstalled}
      />,
    );

    expect(startArtifactPull).not.toHaveBeenCalled();
    expect(screen.getByText(/downloads only after you confirm here/i)).toBeVisible();

    await user.click(screen.getByTestId('engine-artifact-download-button'));
    expect(startArtifactPull).toHaveBeenCalledWith('spacy:en_core_web_sm@3.8.0');
    expect(await screen.findByTestId('fake-pull-complete')).toBeVisible();
    expect(onInstalled).not.toHaveBeenCalled();

    await user.click(screen.getByTestId('fake-pull-complete'));
    expect(onInstalled).toHaveBeenCalledOnce();
  });
});
