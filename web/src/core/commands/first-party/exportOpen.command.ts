import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'export.open'> = {
  match: 'export.open',
  testId: 'workspace-command-export-open',
  enabled: () => true,
  run: (command, ctx) => ctx.actSurface.setExportModal(command.kind),
};

export default descriptor;
