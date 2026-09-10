import type { EditionDescriptor } from '../editions/posture';
import { WALKTHROUGHS, type WalkthroughDefinition } from './walkthroughs';

/** Resolve the complete walkthrough registry for one composed edition. */
export function walkthroughsForEdition(
  edition: Readonly<EditionDescriptor>,
  contributed: readonly WalkthroughDefinition[] = [],
): WalkthroughDefinition[] {
  const candidates = [
    ...WALKTHROUGHS,
    ...contributed,
  ];
  const ids = new Set<string>();
  for (const walkthrough of candidates) {
    if (ids.has(walkthrough.id)) {
      throw new Error(`Duplicate walkthrough id: ${walkthrough.id}`);
    }
    ids.add(walkthrough.id);
  }
  return candidates.filter(
    (walkthrough) => walkthrough.editions === undefined
      || walkthrough.editions.includes(edition.id),
  );
}
