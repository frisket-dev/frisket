// sources.open is dispatched from BOTH runActCommand's 'sources' case
// (today: openSourcesDiscoverTab, no recordCommandAction) AND the ⌘K
// palette's OPEN_SOURCES_COMMAND_DESCRIPTOR (today: openSourcesFromCommand-
// Palette, WITH recordCommandAction — pinned green by
// workbench-command-palette.spec.ts's 'Opened Sources' assertion). The
// union gives both ONE command type, so ONE run() body is possible; the
// palette's richer body is canonical (dropping recordCommandAction would
// fail the pinned spec). DIVERGENCE (documented, not silently absorbed): the
// Act-menu 'sources' path now also fires recordCommandAction, a harmless
// additive lastCommandAction update no spec asserts the absence of.
// openSourcesDiscoverTab (the leaner body) is retired as redundant.
import type { DescriptorFor } from '../types';

const descriptor: DescriptorFor<'sources.open'> = {
  match: 'sources.open',
  testId: 'workspace-command-sources-open',
  enabled: () => true,
  run: (_command, ctx) => ctx.chrome.openSources(),
};

export default descriptor;
