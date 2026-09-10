import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'transcribeCompare.open'> = {
  match: 'transcribeCompare.open',
  testId: 'workspace-command-transcribe-compare-open',
  enabled: () => true,
  run: (_command, ctx) => ctx.scratch.openTranscribeCompare(),
};

export default descriptor;
