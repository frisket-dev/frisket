// Follows the same canonical-superset-body divergence pattern documented in
// sourcesOpen.command.ts.
import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'ocrCompare.open'> = {
  match: 'ocrCompare.open',
  testId: 'workspace-command-ocr-compare-open',
  enabled: () => true,
  run: (_command, ctx) => ctx.scratch.openOcrCompare(),
};

export default descriptor;
