/** @vitest-environment jsdom */
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { artifactRef, blobRef, evidenceArtifact, evidenceSpan } from '../support/evidenceFixtures';

const projectApi = vi.hoisted(() => ({ getEvidenceViewer: vi.fn() }));
vi.mock('../../src/bind/useWorkspaceStores', () => ({ useWorkspaceStores: () => ({ projectApi }) }));
import { SavedTextContext } from '../../src/components/EvidenceTextContext';

const context = {
  text: 'Hello Michigan.\nDetroit replied.', offset_unit: 'utf16_code_unit' as const,
  ranges: [{ span_id: 'entity:1', start: 6, end: 14 }, { span_id: 'entity:2', start: 16, end: 23 }],
  transcript: {
    schema_version: 'frisket.timestamped_text_context.v1' as const,
    evidence_link_stable_id: 'link:transcript', artifact_stable_id: 'audio:1', offset_unit: 'utf16_code_unit' as const,
    segments: [{ span_id: 'time:1', start: 0, end: 15, start_ms: 0, end_ms: 4000 },
      { span_id: 'time:2', start: 16, end: 32, start_ms: 4000, end_ms: 8000 }],
  },
};

beforeEach(() => {
  projectApi.getEvidenceViewer.mockReset();
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('keeps full transcript context and entity highlights alongside its saved source recording', async () => {
  projectApi.getEvidenceViewer.mockResolvedValue({ artifacts: [evidenceArtifact({
    stable_id: 'audio:1', title: 'Council recording', media_type: 'audio/mpeg',
    artifact_ref: artifactRef({ blob: blobRef('/recording.mp3') }),
    spans: [evidenceSpan({ stable_id: 'time:1', clip_url: '/clip/1' }), evidenceSpan({ stable_id: 'time:2', clip_url: '/clip/2' })],
  })] });
  const play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue();
  const { rerender } = render(<SavedTextContext context={context} highlight emphasizedSpanIds={['entity:2']} />);
  const audio = await screen.findByLabelText('Council recording') as HTMLAudioElement;
  expect(audio).toHaveAttribute('src', '/recording.mp3');
  expect(screen.getAllByTestId('evidence-transcript-line')).toHaveLength(2);
  expect(screen.getByText('Hello')).toBeInTheDocument();
  expect(screen.getByText('replied.')).toBeInTheDocument();
  expect(screen.getAllByTestId('evidence-text-highlight')[1]).toHaveTextContent('Detroit');
  expect(screen.getAllByTestId('evidence-text-highlight')[1]).toHaveAttribute('data-emphasized', 'true');
  Object.defineProperty(audio, 'readyState', { value: 1, configurable: true });
  fireEvent.click(screen.getByRole('button', { name: 'Play at 0:04' }));
  expect(audio.currentTime).toBe(4);
  expect(play).toHaveBeenCalledOnce();
  audio.currentTime = 6;
  fireEvent.click(screen.getByRole('button', { name: 'Play at 0:04' }));
  expect(audio.currentTime).toBe(4);
  expect(play).toHaveBeenCalledTimes(2);
  expect(screen.getByRole('link', { name: 'Download clip at 0:04' })).toHaveAttribute('href', '/clip/2');
  rerender(<SavedTextContext context={context} highlight emphasizedSpanIds={['entity:1']} />);
  expect(screen.getByLabelText('Council recording')).toBe(audio);
  expect(projectApi.getEvidenceViewer).toHaveBeenCalledTimes(1);
});

it('preserves saved text and highlights when the recording cannot be loaded', async () => {
  projectApi.getEvidenceViewer.mockRejectedValue(new Error('missing'));
  render(<SavedTextContext context={context} highlight emphasizedSpanIds={[]} />);
  expect(await screen.findByText(/Recording unavailable/)).toBeInTheDocument();
  expect(screen.getAllByTestId('evidence-text-highlight')).toHaveLength(2);
  expect(screen.getByRole('button', { name: 'Play at 0:04' })).toBeDisabled();
});
