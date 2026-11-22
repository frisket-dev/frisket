export const TEAM_SPEC_BASENAMES = [
  'diagnostic-bundle.spec.ts',
] as const;

export const TEAM_SPEC_PATTERNS = TEAM_SPEC_BASENAMES.map(
  (name) => new RegExp(`(^|/)${name.replace(/\./g, '\\.')}$`),
);
