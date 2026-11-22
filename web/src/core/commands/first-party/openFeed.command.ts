import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'openFeed'> = {
  match: 'openFeed',
  testId: 'workspace-command-open-feed',
  enabled: () => true,
  run: (command, ctx) => ctx.route.openFeed(command.feedId),
};

export default descriptor;
