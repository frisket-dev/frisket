// Grid is always available regardless of every other input — it is the base
// view every sheet can render.
import type { WorkViewDescriptor } from './types';

const descriptor: WorkViewDescriptor<'grid'> = {
  kind: 'grid',
  // No title: promoteCurrentView never labels the grid view (it returns
  // early for activeWorkView === 'grid') — see types.ts's doc comment.
  computeEntry: () => ({ available: true, status: 'enabled', reason: 'available' }),
};

export default descriptor;
