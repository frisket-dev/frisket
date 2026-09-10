import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'openMainViewContribution'> = {
  match: 'openMainViewContribution',
  testId: 'workspace-command-open-main-view-contribution',
  enabled: () => true,
  run: (command, ctx) => ctx.chrome.openMainViewContribution(command.contributionId),
};

export default descriptor;
