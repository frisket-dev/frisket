import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'openActionRoute'> = {
  match: 'openActionRoute',
  testId: 'workspace-command-open-action-route',
  enabled: () => true,
  run: (command, ctx) => ctx.route.openActionRoute(command.actionKind),
};

export default descriptor;
