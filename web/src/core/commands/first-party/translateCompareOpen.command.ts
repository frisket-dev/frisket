import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'translateCompare.open'> = {
  match: 'translateCompare.open',
  testId: 'workspace-command-translate-compare-open',
  enabled: () => true,
  run: (_command, ctx) => ctx.scratch.openTranslateCompare(),
};

export default descriptor;
