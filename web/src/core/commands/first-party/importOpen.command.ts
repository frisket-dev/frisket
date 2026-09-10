import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'import.open'> = {
  match: 'import.open',
  testId: 'workspace-command-import-open',
  enabled: () => true,
  run: (_command, ctx) => ctx.actSurface.openImportDialog(),
};

export default descriptor;
