import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'openSheet'> = {
  match: 'openSheet',
  testId: 'workspace-command-open-sheet',
  enabled: () => true,
  run: (command, ctx) => ctx.route.openSheet(command.sheetId),
};

export default descriptor;
