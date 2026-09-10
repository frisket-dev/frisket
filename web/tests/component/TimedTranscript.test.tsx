// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  hasTimedTranscript,
  timedTranscriptValue,
  type TimedTranscriptDocument,
} from '../../src/workbench/timedTranscriptModel';
import { TimedTranscript } from '../../src/workbench/TimedTranscript';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const document: TimedTranscriptDocument = {
  row: {
    id: 'row-1',
    index: 0,
    cells: {
      '2': 'Opening remarks. Contract discussion.',
      '3': JSON.stringify([
        { segment_index: 0, start: 0, end: 4.5, text: 'Opening remarks.', speaker: 'Chair' },
        { segment_index: 1, start: 4.5, end: 9, text: 'Contract discussion.' },
      ]),
    },
    provenance: {},
  },
  sheet: {
    columns: [
      { id: '1', name: 'recording', type: 'audio', ai_generated: false },
      { id: '2', name: 'transcript', type: 'timestamped_transcript', ai_generated: true },
      { id: '3', name: 'transcript_segments', type: 'json', ai_generated: true },
    ],
  },
  media: { url: '/recording.mp3', label: 'meeting.mp3', mime: 'audio/mpeg' },
  kind: 'audio',
  title: 'Council meeting',
};

describe('TimedTranscript', () => {
  it('pulls the transcript pair out of the document object', () => {
    expect(timedTranscriptValue(document)).toEqual({
      text: 'Opening remarks. Contract discussion.',
      segments: document.row.cells['3'],
    });
    expect(hasTimedTranscript(document)).toBe(true);
  });

  it('renders the document media and seekable transcript together', () => {
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue();
    render(<TimedTranscript document={document} />);

    const audio = screen.getByLabelText('Council meeting') as HTMLAudioElement;
    expect(audio).toHaveAttribute('src', '/recording.mp3');
    const segments = screen.getAllByTestId('document-transcript-segment');
    expect(segments).toHaveLength(2);
    expect(segments[0]).toHaveTextContent('Chair: Opening remarks.');

    fireEvent.click(segments[1]);
    expect(audio.currentTime).toBe(4.5);
    expect(play).toHaveBeenCalledOnce();
    expect(segments[1]).toHaveAttribute('data-active', 'true');
  });
});
