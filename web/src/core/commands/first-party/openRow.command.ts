import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'openRow'> = {
  match: 'openRow',
  testId: 'workspace-command-open-row',
  enabled: () => true,
  run: (command, ctx) => ctx.route.openRow(command.sheetId, command.rowId, command.columnId),
};

export default descriptor;
