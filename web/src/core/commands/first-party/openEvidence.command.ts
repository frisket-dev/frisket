import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'openEvidence'> = {
  match: 'openEvidence',
  testId: 'workspace-command-open-evidence',
  enabled: () => true,
  run: (command, ctx) => ctx.chrome.openEvidence(command.linkId, command.host),
};

export default descriptor;
