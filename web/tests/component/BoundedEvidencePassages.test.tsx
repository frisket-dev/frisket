// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ArtifactSource } from '../../src/components/evidence/ArtifactSource';
import { TextArtifactSource } from '../../src/components/evidence/EvidenceTextSource';
import { createProjectApi } from '../../src/api/real';
import type { EvidenceArtifact, EvidenceViewerPayload } from '../../src/api/types';
import { evidenceArtifact, evidenceSpan, artifactRef, blobRef } from '../support/evidenceFixtures';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('bounded-evidence');
const read = vi.spyOn(api, 'getEvidenceViewer');
const { render } = createWorkspaceTestHarness({ projectId: 'bounded-evidence', api: { projectApi: api } });
function context(start = 0, spanId = 'span:first', text = '😀 quoted head', revision: number | null = 9) {
  const end = start + Array.from(text).length;
  return { text, offset_unit: 'utf16_code_unit' as const, content_hash: 'sha256:source',
    evidence_link_id: 'owner:a', artifact_stable_id: 'artifact:22',
    ranges: [{ span_id: spanId, start: 3, end: 9, source_start: start + 2, source_end: start + 8 }],
    window: { start, end, total_characters: 100000, total_bytes: 100003, retrieval_revision: revision,
      previous_start: start ? Math.max(0, start - 16384) : null, next_start: end < 100000 ? end : null,
      reached_start: start === 0, reached_end: end >= 100000 } };
}
function source(overrides: Partial<EvidenceArtifact> = {}) {
  return evidenceArtifact({ id: 22, stable_id: 'artifact:22', media_type: 'text/plain',
    spans: [evidenceSpan({ stable_id: 'span:first', quote: 'First citation' }), evidenceSpan({ stable_id: 'span:tail', quote: 'Tail citation' })],
    text_context: context(), ...overrides });
}
function response(artifact: EvidenceArtifact) { return { artifacts: [artifact] } as EvidenceViewerPayload; }
beforeEach(() => { Element.prototype.scrollIntoView = vi.fn(); });
afterEach(() => { cleanup(); read.mockReset(); });

describe('bounded saved evidence passages', () => {
  it('keeps a dropdown-selected citation and its owner after navigating to the next passage', async () => {
    const selected = { ...context(90000, 'span:tail', '😀 quoted tail'), evidence_link_id: 'owner:b' };
    const following = { ...context(90013, 'span:tail', '😀 quoted next'), evidence_link_id: 'owner:b',
      ranges: [
        { span_id: 'span:tail', start: 3, end: 9 },
        { span_id: 'span:first', start: 10, end: 14 },
      ] };
    read.mockResolvedValueOnce(response(source({ text_context: selected })))
      .mockResolvedValueOnce(response(source({ text_context: following })));
    render(<TextArtifactSource artifact={source()} scopeSpanId="span:first" textPassageTargets={[
      { spanId: 'span:first', evidenceLinkId: 'owner:a', artifactId: 22 },
      { spanId: 'span:tail', evidenceLinkId: 'owner:b', artifactId: 22 },
    ]} />);
    fireEvent.change(screen.getByRole('combobox', { name: 'Citation passage' }), { target: { value: 'span:tail' } });
    await waitFor(() => expect(screen.getByTestId('evidence-text-body').textContent).toBe('😀 quoted tail'));
    expect(read).toHaveBeenLastCalledWith('owner:b', expect.objectContaining({ textSpanId: 'span:tail' }));
    fireEvent.click(screen.getByRole('button', { name: 'Next passage' }));
    await waitFor(() => expect(screen.getByTestId('evidence-text-body').textContent).toBe('😀 quoted next'));
    expect(read).toHaveBeenLastCalledWith('owner:b', expect.objectContaining({ textStart: 90013, textRevision: 9 }));
    expect(screen.getByRole('combobox', { name: 'Citation passage' })).toHaveValue('span:tail');
    expect(screen.getByTestId('evidence-text-highlight')).toHaveTextContent('quoted');
    expect(screen.getByTestId('evidence-text-highlight')).toHaveAttribute('data-emphasized', 'true');
  });

  it('preserves explicit current-source recovery while loading the next passage', async () => {
    const recovered = { ...context(), recovered: true };
    read.mockResolvedValue(response(source({ text_context: { ...context(13, 'span:first', '😀 quoted next'), recovered: true } })));
    render(<TextArtifactSource artifact={source({ text_context: recovered })} />);
    fireEvent.click(screen.getByRole('button', { name: 'Next passage' }));
    await waitFor(() => expect(screen.getByTestId('evidence-text-body').textContent).toBe('😀 quoted next'));
    expect(read).toHaveBeenCalledWith('owner:a', expect.objectContaining({ locateCurrent: true, textRevision: 9 }));
  });

  it('describes an incomplete recovery scan without claiming that the passage is absent', () => {
    render(<ArtifactSource artifact={source({ text_context: null, text_context_status: 'stale',
      recovery_status: 'incomplete', recovery_notice: 'The source was only partly searched.', recovery_scanned_characters: 1000 })} />);
    expect(screen.getByTestId('evidence-text-recovery-incomplete')).toHaveTextContent('Searched 1,000 characters.');
    expect(screen.queryByTestId('evidence-text-recovery-not-found')).not.toBeInTheDocument();
  });

  it('navigates revision-pinned passages and uses local UTF-16 highlight ranges after an emoji', async () => {
    const initial = source();
    read.mockResolvedValue(response(source({ text_context: context(13, 'span:first', '😀 quoted next') })));
    render(<TextArtifactSource artifact={initial} />);
    expect(screen.getByTestId('evidence-text-highlight')).toHaveTextContent('quoted');
    expect(read).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Next passage' }));
    await waitFor(() => expect(screen.getByTestId('evidence-text-body').textContent).toBe('😀 quoted next'));
    expect(read).toHaveBeenCalledWith('owner:a', expect.objectContaining({ textArtifactId: 22, textStart: 13, textRevision: 9, textLimit: 16384 }));
    expect(screen.getByTestId('evidence-text-body').textContent).toBe('😀 quoted next');
    expect(screen.getByTestId('evidence-text-highlight')).toHaveTextContent('quoted');
    expect(screen.getByRole('button', { name: 'Previous passage' })).toBeEnabled();
  });

  it('jumps to an offscreen cited span using its owning link and span ID', async () => {
    read.mockResolvedValue(response(source({ text_context: context(90000, 'span:tail', '😀 quoted tail') })));
    render(<TextArtifactSource artifact={source()} scopeSpanId="span:tail" />);
    await screen.findByTestId('evidence-text-body');
    expect(read).toHaveBeenCalledWith('owner:a', expect.objectContaining({ textArtifactId: 22, textSpanId: 'span:tail' }));
    expect(screen.getByTestId('evidence-text-body').textContent).toBe('😀 quoted tail');
  });

  it('does not display stale passage highlights and reloads without the rejected revision', async () => {
    read.mockRejectedValueOnce(Object.assign(new Error('changed'), { status: 409 }))
      .mockResolvedValueOnce(response(source({ text_context: context(0, 'span:first', '😀 quoted new', 10) })));
    render(<TextArtifactSource artifact={source()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Next passage' }));
    await screen.findByText('The source text changed. Reload the passage to continue.');
    expect(screen.queryByTestId('evidence-text-highlight')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Reload passage' }));
    await screen.findByTestId('evidence-text-body');
    expect(read).toHaveBeenLastCalledWith('owner:a', expect.objectContaining({ textRevision: undefined }));
    expect(screen.getByTestId('evidence-text-body').textContent).toBe('😀 quoted new');
  });

  it('aborts a superseded citation jump and rejects its late response', async () => {
    let finish!: (payload: EvidenceViewerPayload) => void;
    read.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const mounted = render(<TextArtifactSource artifact={source()} scopeSpanId="span:tail" />);
    const signal = read.mock.calls[0][1]?.signal;
    const next = source({ text_context: { ...context(10, 'span:tail', '😀 quoted other'), evidence_link_id: 'owner:b' } });
    mounted.rerender(<TextArtifactSource artifact={next} scopeSpanId="span:tail" />);
    expect(signal?.aborted).toBe(true);
    await act(async () => finish(response(source({ text_context: context(90000, 'span:tail', '😀 quoted obsolete') }))));
    expect(screen.getByTestId('evidence-text-body').textContent).toBe('😀 quoted other');
  });

  it('keeps the recording player and clip links while navigating timestamped passages', async () => {
    const makeTranscript = (start: number, end: number) => ({ schema_version: 'frisket.timestamped_text_context.v1' as const,
      evidence_link_stable_id: 'recording-owner', artifact_stable_id: 'recording', offset_unit: 'utf16_code_unit' as const,
      segments: [{ span_id: 'record-span', start: 0, end, start_ms: start, end_ms: start + 10000, speaker: 'Speaker' }] });
    const initialText = context();
    const nextText = context(13, 'span:first', '😀 quoted next');
    const initial = source({ text_context: { ...initialText, transcript: makeTranscript(90000, initialText.text.length) } });
    const recording = evidenceArtifact({ stable_id: 'recording', media_type: 'audio/mpeg', title: 'Recording',
      artifact_ref: artifactRef({ blob: blobRef('/recording.mp3') }),
      spans: [evidenceSpan({ stable_id: 'record-span', clip_url: '/clips/passage' })] });
    read.mockImplementation(async (link) => link === 'recording-owner' ? response(recording)
      : response(source({ text_context: { ...nextText, transcript: makeTranscript(180000, nextText.text.length) } })));
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
    render(<TextArtifactSource artifact={initial} playFocusedPassage emphasizedSpanIds={['span:first']} />);
    const player = await screen.findByLabelText('Recording');
    Object.defineProperty(player, 'readyState', { value: 1, configurable: true });
    await screen.findByRole('link', { name: 'Download clip at 1:30' });
    fireEvent.click(screen.getByRole('button', { name: 'Next passage' }));
    await screen.findByRole('button', { name: 'Play at 3:00' });
    expect(screen.getByLabelText('Recording')).toBe(player);
    expect(screen.getByRole('link', { name: 'Download clip at 3:00' })).toHaveAttribute('href', '/clips/passage');
    expect(read.mock.calls.filter(([link]) => link === 'recording-owner')).toHaveLength(1);
    await waitFor(() => expect((player as HTMLMediaElement).currentTime).toBe(180));
    expect(play).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Play at 3:00' }));
    await waitFor(() => expect((player as HTMLMediaElement).currentTime).toBe(180));
    expect(play).toHaveBeenCalled();
    play.mockRestore();
  });
});
