// Extraction operates on visual documents only. Annotated text can be read in
// Document view, but is not itself an extraction source.
import type { WorkViewDescriptor } from './types';

const descriptor: WorkViewDescriptor<'extract'> = {
  kind: 'extract',
  title: 'Extract',
  computeEntry: (input) => {
    const available = input.firstExtractSourceColumn !== null;
    return {
      available,
      status: available ? 'enabled' : 'disabled',
      reason: available ? 'available' : 'data_requirements_unmet',
    };
  },
};

export default descriptor;
