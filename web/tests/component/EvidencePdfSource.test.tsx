// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { ArtifactSource } from '../../src/components/EvidenceViewer';
import { artifactRef, blobRef, evidenceArtifact } from '../support/evidenceFixtures';

afterEach(cleanup);

describe('evidence PDF renderer', () => {
  it('renders the original PDF blob instead of the unsupported-artifact fallback', () => {
    const artifact = evidenceArtifact({
      artifact_kind: 'file',
      media_type: 'application/pdf',
      filename: 'filing.pdf',
      artifact_ref: artifactRef({
        artifact_kind: 'file',
        media_type: 'application/pdf',
        blob: blobRef('/api/projects/p/blobs/pdf', { filename: 'filing.pdf' }),
      }),
    });

    render(<ArtifactSource artifact={artifact} />);

    expect(screen.getByTestId('evidence-pdf')).toHaveAttribute(
      'src',
      '/api/projects/p/blobs/pdf',
    );
    expect(screen.queryByTestId('evidence-artifact-fallback')).toBeNull();
  });
});
