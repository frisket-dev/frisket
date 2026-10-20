import type { Meta, StoryObj } from '@storybook/react-vite';
import { TimedTranscript } from '../src/workbench/TimedTranscript';
import type { TimedTranscriptDocument } from '../src/workbench/timedTranscriptModel';
import audioUrl from '../tests/fixtures/media/tiny-audio.wav?url';
import videoUrl from '../tests/fixtures/media/tiny-video.mp4?url';

const meta = { title: 'Inventory/Timed transcript' } satisfies Meta;
export default meta;
type Story = StoryObj<typeof meta>;

function Transcript({ kind }: { kind: 'audio' | 'video' }) {
  const document: TimedTranscriptDocument = {
    kind, title: 'Fixture interview', media: { url: kind === 'audio' ? audioUrl : videoUrl, label: 'Fixture interview' },
    sheet: { columns: [
      { id: 'text', name: 'transcript', type: 'timestamped_transcript' },
      { id: 'segments', name: 'transcript_segments', type: 'json' },
    ] },
    row: { id: 'fixture', index: 0, provenance: {}, cells: {
      text: 'We reviewed the contract. The council approved it.',
      segments: JSON.stringify([
        { start: 0, end: 0.8, text: 'We reviewed the contract.', speaker: 'Interviewer' },
        { start: 0.8, end: 1.5, text: 'The council approved it.', speaker: 'Respondent' },
      ]),
    } },
  };
  return <section className="inventory-example" style={{ maxWidth: 800 }}>
    <h2>{kind === 'audio' ? 'Audio' : 'Video'} transcript</h2>
    <p>Real player/transcript component; synthetic segments and bundled test media, not a model run.</p>
    <TimedTranscript document={document} />
  </section>;
}

export const Audio: Story = { render: () => <Transcript kind="audio" /> };
export const Video: Story = { render: () => <Transcript kind="video" /> };
