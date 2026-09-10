export const TEAM_SPEC_BASENAMES = [
  'account-organization.spec.ts',
  'admin-debug-errors.spec.ts',
  'admin-users.spec.ts',
  'audit-log-viewer.spec.ts',
  'diagnostic-bundle.spec.ts',
  'project-collaboration.spec.ts',
  'settings-account-organization.spec.ts',
] as const;

export const TEAM_SPEC_PATTERNS = TEAM_SPEC_BASENAMES.map(
  (name) => new RegExp(`(^|/)${name.replace(/\./g, '\\.')}$`),
);
