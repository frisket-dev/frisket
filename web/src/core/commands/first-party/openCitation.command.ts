import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'openCitation'> = {
  match: 'openCitation',
  testId: 'workspace-command-open-citation',
  enabled: () => true,
  run: (command, ctx) => ctx.chrome.openCitation(command.linkId, command.host),
};

export default descriptor;
