/// <reference types="vite/client" />
// eslint-disable-next-line @typescript-eslint/triple-slash-reference
/// <reference path="./popover-attribute.d.ts" />
// eslint-disable-next-line @typescript-eslint/triple-slash-reference
/// <reference path="./prismjs-core.d.ts" />
// eslint-disable-next-line @typescript-eslint/triple-slash-reference
/// <reference path="./bind/use-sync-external-store-with-selector.d.ts" />

export { getAdminUsers } from './api/real';
export type { AdminUsers } from './api/adminBrowser';
export { AuthForm, AuthScreen } from './components/AuthScreen';
export { AdminShell } from './components/AdminPage';
export { defineEditionModule } from './editions/module';
export type {
  AdminOverviewSlotProps,
  EditionModule,
  EditionModuleDefinition,
  WatchControlSlotProps,
} from './editions/module';
export type {
  EditionDescriptor,
  EditionPosture,
  PostureCapabilities,
} from './editions/posture';
export { OpenEditionRoot } from './entries/OpenEditionRoot';
export { mountEdition } from './entries/mount';
export type { EditionRoute } from './routes/openRoutes';
export { useShellIdentity, type ShellIdentity } from './shellIdentity';
export type {
  EditionSettingsRenderProps,
  EditionSettingsSection,
} from './settings/openSettingsRegistry';
export type {
  WalkthroughAdvance,
  WalkthroughDefinition,
  WalkthroughInstruction,
  WalkthroughRichInstruction,
  WalkthroughStep,
  WalkthroughTarget,
} from './walkthrough/walkthroughs';

// Reuse the existing author-facing SDK contract; the host package does not
// fork, widen, or copy those types.
export type * from '@frisket/plugin-sdk';
