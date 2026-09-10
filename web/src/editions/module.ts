import type { ComponentType } from 'react';

import type { AdminOverview, WatchInfo } from '../api/types';
import { routesFor, type EditionRoute } from '../routes/openRoutes';
import {
  settingsSectionsFor,
  type ActiveSettingsSection,
  type EditionSettingsSection,
} from '../settings/openSettingsRegistry';
import { walkthroughsForEdition } from '../walkthrough/registry';
import type {
  WalkthroughDefinition,
  WalkthroughInstruction,
  WalkthroughStep,
  WalkthroughTarget,
} from '../walkthrough/walkthroughs';
import {
  immutableEditionDescriptor,
  type EditionDescriptor,
} from './posture';

export { EditionModuleProvider } from './moduleProvider';
export { useEditionModule } from './moduleContext';

export interface AdminOverviewSlotProps {
  readonly overview: Readonly<AdminOverview>;
}

export interface WatchControlSlotProps {
  readonly projectId: string;
  readonly watch: Readonly<WatchInfo>;
  readonly refreshWatches: () => void;
}

export interface EditionModuleDefinition {
  readonly descriptor: EditionDescriptor;
  readonly routes?: readonly EditionRoute[];
  readonly settingsSections?: readonly EditionSettingsSection[];
  readonly walkthroughs?: readonly WalkthroughDefinition[];
  readonly AdminOverviewSlot?: ComponentType<AdminOverviewSlotProps>;
  readonly WatchControlSlot?: ComponentType<WatchControlSlotProps>;
}

/** One immutable, fully resolved edition passed to the application mount. */
export interface EditionModule {
  readonly descriptor: Readonly<EditionDescriptor>;
  readonly routes: readonly EditionRoute[];
  readonly settingsSections: readonly ActiveSettingsSection[];
  readonly walkthroughs: readonly WalkthroughDefinition[];
  readonly AdminOverviewSlot?: ComponentType<AdminOverviewSlotProps>;
  readonly WatchControlSlot?: ComponentType<WatchControlSlotProps>;
}

function immutableRoute(route: EditionRoute): EditionRoute {
  return Object.freeze({ ...route });
}

function immutableSettingsSection(
  section: ActiveSettingsSection,
): ActiveSettingsSection {
  return Object.freeze({
    ...section,
    searchLabels: Object.freeze([...section.searchLabels]),
  });
}

function immutableWalkthroughTarget(
  target: WalkthroughTarget,
): WalkthroughTarget {
  if (target.kind === 'completed-run') {
    return Object.freeze({
      kind: target.kind,
      target: immutableWalkthroughTarget(target.target),
    });
  }
  return Object.freeze({ ...target });
}

function immutableWalkthroughInstruction(
  instruction: WalkthroughInstruction,
): WalkthroughInstruction {
  if (typeof instruction === 'string') return instruction;
  return Object.freeze({
    ...instruction,
    ...(instruction.learnMore
      ? { learnMore: Object.freeze({ ...instruction.learnMore }) }
      : {}),
  });
}

function immutableWalkthroughStep(step: WalkthroughStep): WalkthroughStep {
  return Object.freeze({
    ...step,
    instruction: immutableWalkthroughInstruction(step.instruction),
    target: immutableWalkthroughTarget(step.target),
  });
}

function immutableWalkthrough(
  walkthrough: WalkthroughDefinition,
): WalkthroughDefinition {
  return Object.freeze({
    ...walkthrough,
    ...(walkthrough.editions
      ? { editions: Object.freeze([...walkthrough.editions]) }
      : {}),
    steps: Object.freeze(walkthrough.steps.map(immutableWalkthroughStep)),
  });
}

/** Resolve and freeze an edition once, before React mounts. */
export function defineEditionModule(
  definition: EditionModuleDefinition,
): Readonly<EditionModule> {
  const descriptor = immutableEditionDescriptor(definition.descriptor);
  return Object.freeze({
    descriptor,
    routes: Object.freeze(
      routesFor(descriptor, definition.routes).map(immutableRoute),
    ),
    settingsSections: Object.freeze(
      settingsSectionsFor(descriptor, definition.settingsSections)
        .map(immutableSettingsSection),
    ),
    walkthroughs: Object.freeze(
      walkthroughsForEdition(descriptor, definition.walkthroughs)
        .map(immutableWalkthrough),
    ),
    AdminOverviewSlot: definition.AdminOverviewSlot,
    WatchControlSlot: definition.WatchControlSlot,
  });
}
