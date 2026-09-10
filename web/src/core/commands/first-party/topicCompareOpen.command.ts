import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'topicCompare.open'> = {
  match: 'topicCompare.open',
  testId: 'workspace-command-topic-compare-open',
  enabled: () => true,
  run: (_command, ctx) => ctx.scratch.openTopicCompare(),
};

export default descriptor;
