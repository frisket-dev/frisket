// Availability: hidden_by_profile / data_requirements_unmet / available.
import type { WorkViewDescriptor } from './types';

const descriptor: WorkViewDescriptor<'graph'> = {
  kind: 'graph',
  title: 'Graph',
  computeEntry: (input) => {
    const graphHidden = input.hiddenContributionIds.has(input.contributionIds.graphNeighborhood);
    const available = !graphHidden && input.sheetIsEdgeShaped;
    const reason = graphHidden
      ? 'hidden_by_profile'
      : input.sheetIsEdgeShaped
        ? 'available'
        : 'data_requirements_unmet';
    return { available, status: available ? 'enabled' : 'disabled', reason };
  },
};

export default descriptor;
