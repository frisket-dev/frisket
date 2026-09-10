// Intentionally does NOT consult hidden-ness — this is parity with existing
// behavior, not an oversight. The switcher's gallery segment is
// `imageGalleryAvailability.available` with no hidden-id check at all; only the
// AMBIENT gallery PANE suppression (`galleryPaneShowing`, useWorkspaceModel.tsx)
// checks isContributionHidden separately.
import type { WorkViewDescriptor } from './types';

const descriptor: WorkViewDescriptor<'gallery'> = {
  kind: 'gallery',
  title: 'Gallery',
  computeEntry: (input) => {
    const available = input.imageGalleryAvailable;
    return {
      available,
      status: available ? 'enabled' : 'disabled',
      reason: available ? 'available' : 'data_requirements_unmet',
    };
  },
};

export default descriptor;
