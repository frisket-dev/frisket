/** @vitest-environment jsdom */
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ArtifactSource } from '../../src/components/EvidenceViewer';
import { artifactRef, blobRef, evidenceArtifact, evidenceSpan } from '../support/evidenceFixtures';

beforeEach(() => { Element.prototype.scrollIntoView = vi.fn(); });
afterEach(cleanup);

function mediaArtifact(mediaType: string) {
  return evidenceArtifact({ media_type: mediaType, title: 'Source', artifact_ref: artifactRef({ blob: blobRef('/source') }) });
}

describe('ArtifactSource media coverage', () => {
  it('renders a standalone image with its recorded regions', () => {
    const artifact = mediaArtifact('image/png');
    artifact.spans = [evidenceSpan({ stable_id: 'face', span_kind: 'region', selector: {
      bbox: [{ space: 'page_normalized', x0: 0.1, y0: 0.2, x1: 0.5, y1: 0.6 }],
    }, quote: 'Person' })];
    render(<ArtifactSource artifact={artifact} emphasizedSpanIds={['face']} />);
    expect(screen.getByRole('img', { name: 'Source' })).toHaveAttribute('src', '/source');
    expect(screen.getByTestId('evidence-region-highlight')).toHaveAttribute('data-emphasized', 'true');
  });

  it('shows a video detection only during its recorded time range', () => {
    const artifact = mediaArtifact('video/mp4');
    artifact.spans = [evidenceSpan({ stable_id: 'detection', span_kind: 'temporal', selector: {
      start_ms: 4000, end_ms: 8000,
      bbox: [{ space: 'frame_normalized', x0: 0.1, y0: 0.2, x1: 0.5, y1: 0.6 }],
    } })];
    const { container } = render(<ArtifactSource artifact={artifact} emphasizedSpanIds={['detection']} />);
    const video = container.querySelector('video')!;
    expect(screen.queryByTestId('evidence-region-highlight')).not.toBeInTheDocument();
    video.currentTime = 5;
    fireEvent.timeUpdate(video);
    expect(screen.getByTestId('evidence-region-highlight')).toHaveAttribute('data-emphasized', 'true');
    video.currentTime = 9;
    fireEvent.timeUpdate(video);
    expect(screen.queryByTestId('evidence-region-highlight')).not.toBeInTheDocument();
  });

  it('keeps native PDF preview when no rendered pages were saved', () => {
    render(<ArtifactSource artifact={mediaArtifact('application/pdf')} />);
    expect(screen.getByTitle('Evidence PDF: Source')).toHaveAttribute('src', '/source');
  });

  it('shows source text around a highlighted citation instead of only the quote', () => {
    const artifact = evidenceArtifact({ media_type: 'text/plain', text_context: {
      text: 'Intro\nMichigan spoke.\nConclusion', offset_unit: 'utf16_code_unit',
      ranges: [{ span_id: 'entity', start: 6, end: 14 }],
    } });
    render(<ArtifactSource artifact={artifact} emphasizedSpanIds={['entity']} />);
    expect(screen.getByTestId('evidence-text-body')).toHaveTextContent('Intro Michigan spoke. Conclusion');
    expect(screen.getByTestId('evidence-text-highlight')).toHaveTextContent('Michigan');
    expect(screen.getByTestId('evidence-text-highlight')).toHaveAttribute('data-emphasized', 'true');
  });

  it.each(['audio/mpeg', 'video/mp4'])('renders %s with separate timestamped segments and working seek', (mediaType) => {
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue();
    const artifact = mediaArtifact(mediaType);
    artifact.spans = [0, 4].map((seconds, index) => evidenceSpan({
      span_kind: 'temporal', selector: { start_ms: seconds * 1000, end_ms: seconds * 1000 + 3000 },
      quote: `Speech ${index}`, clip_url: `/clip/${index}`,
    }));
    const { container } = render(<ArtifactSource artifact={artifact} />);
    expect(screen.getAllByTestId('evidence-transcript-line')).toHaveLength(2);
    const media = container.querySelector('audio,video') as HTMLMediaElement;
    Object.defineProperty(media, 'readyState', { configurable: true, value: 1 });
    fireEvent.click(screen.getAllByTestId('evidence-temporal-segment')[1]);
    expect(media.currentTime).toBe(4);
    expect(play).toHaveBeenCalled();
    expect(screen.getAllByTitle('Download this clip')).toHaveLength(2);
    play.mockRestore();
  });
});
