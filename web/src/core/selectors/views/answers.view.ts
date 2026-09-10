// The Grounded Answers reading view offers itself iff the sheet carries at
// least one column with active evidence (SheetMeta.citedColumnIds). Mirrors
// document.view.ts's data-keyed shape (a plain non-empty check, no
// contribution/hidden-ness concept — there is no plugin behind this view).
import type { WorkViewDescriptor } from './types';

const descriptor: WorkViewDescriptor<'answers'> = {
  kind: 'answers',
  title: 'Answers',
  computeEntry: (input) => {
    const available = input.citedColumnIds.length > 0;
    return {
      available,
      status: available ? 'enabled' : 'disabled',
      reason: available ? 'available' : 'data_requirements_unmet',
    };
  },
};

export default descriptor;
