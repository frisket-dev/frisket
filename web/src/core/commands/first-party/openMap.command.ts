import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'openMap'> = {
  match: 'openMap',
  testId: 'workspace-command-open-map',
  enabled: () => true,
  run: (command, ctx) => ctx.chrome.openMap(command.columnId),
};

export default descriptor;
