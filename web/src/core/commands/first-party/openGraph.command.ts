import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'openGraph'> = {
  match: 'openGraph',
  testId: 'workspace-command-open-graph',
  enabled: () => true,
  run: (_command, ctx) => ctx.chrome.openGraph(),
};

export default descriptor;
