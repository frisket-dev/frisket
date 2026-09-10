// Available iff the sheet can be READ as a document: a media/file column, or a
// text column that carries annotation layers.
//
// The second arm is the annotated-text reader's entry point. It is gated on the
// NARROW `annotatedTextColumnIds` signal rather than "has any text column"
// because every sheet has a text column — that rule would offer the Document
// view (and RowDrawer's "Open in Document view") everywhere, for sheets whose
// reader would have nothing to show.
//
// The consequence is deliberate and worth stating: a paste/text-only sheet is
// reachable as a document once something has annotated it, and not before. That
// is what makes extracted entities readable IN CONTEXT regardless of how the
// rows were imported — previously such a sheet offered only Grid and Answers.
import type { WorkViewDescriptor } from './types';

const descriptor: WorkViewDescriptor<'document'> = {
  kind: 'document',
  title: 'Document',
  computeEntry: (input) => {
    const available =
      input.firstMediaColumn !== null || input.annotatedTextColumnIds.length > 0;
    return {
      available,
      status: available ? 'enabled' : 'disabled',
      reason: available ? 'available' : 'data_requirements_unmet',
    };
  },
};

export default descriptor;
