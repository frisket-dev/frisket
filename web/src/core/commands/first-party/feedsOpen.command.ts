import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'feeds.open'> = {
  match: 'feeds.open',
  testId: 'workspace-command-feeds-open',
  enabled: () => true,
  run: (_command, ctx) => ctx.chrome.openFeeds(),
};

export default descriptor;
