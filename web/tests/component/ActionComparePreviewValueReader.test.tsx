// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ActionComparisonPreviewArtifactValue } from '../../src/api/actionComparison';
import {
  ACTION_COMPARE_PREVIEW_UNAVAILABLE_MESSAGE,
} from '../../src/workbench/actionCompareSession';
import { PreviewValueReader } from '../../src/workspace/WorkspaceActionCompareView';

const value: ActionComparisonPreviewArtifactValue = {
  kind: 'preview_artifact', artifact_id: 'artifact-1', href: '/preview/artifact-1',
  preview: 'Long preview', value_kind: 'text', total_characters: 100_000,
  total_bytes: 100_000, truncated: true,
};

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Action Compare preview value reader', () => {
  it('marks only a missing known preview artifact unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 404 })));
    const onUnavailable = vi.fn();

    render(<PreviewValueReader value={value} previewId="preview-B"
      onUnavailable={onUnavailable} />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      ACTION_COMPARE_PREVIEW_UNAVAILABLE_MESSAGE,
    );
    expect(onUnavailable).toHaveBeenCalledOnce();
    expect(onUnavailable).toHaveBeenCalledWith('preview-B');
  });

  it('preserves authorization failures without making the preview retryable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 403 })));
    const onUnavailable = vi.fn();

    render(<PreviewValueReader value={value} previewId="preview-B"
      onUnavailable={onUnavailable} />);

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load this file (403).');
    expect(onUnavailable).not.toHaveBeenCalled();
  });
});
