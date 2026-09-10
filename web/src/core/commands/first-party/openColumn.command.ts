import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'openColumn'> = {
  match: 'openColumn',
  testId: 'workspace-command-open-column',
  enabled: () => true,
  run: (command, ctx) => ctx.route.openColumn(command.columnId),
};

export default descriptor;
