// Availability precedence: missing_contribution / hidden_by_profile /
// data_requirements_unmet / available, in that order.
import type { WorkViewDescriptor } from './types';

const descriptor: WorkViewDescriptor<'map'> = {
  kind: 'map',
  title: 'Map',
  computeEntry: (input) => {
    const mapHidden =
      input.mapContributionId !== null && input.hiddenContributionIds.has(input.mapContributionId);
    const reason =
      input.mapContributionId === null
        ? 'missing_contribution'
        : mapHidden
          ? 'hidden_by_profile'
          : input.firstGeoColumn === null
            ? 'data_requirements_unmet'
            : 'available';
    const available = reason === 'available';
    return { available, status: available ? 'enabled' : 'disabled', reason };
  },
};

export default descriptor;
