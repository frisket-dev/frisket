import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'settings.open'> = {
  match: 'settings.open',
  testId: 'workspace-command-settings-open',
  enabled: () => true,
  run: (_command, ctx) => ctx.chrome.openSettings(),
};

export default descriptor;
