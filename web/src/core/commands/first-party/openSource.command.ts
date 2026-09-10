import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'openSource'> = {
  match: 'openSource',
  testId: 'workspace-command-open-source',
  enabled: () => true,
  run: (command, ctx) => ctx.route.openSource(command.sourceId),
};

export default descriptor;
