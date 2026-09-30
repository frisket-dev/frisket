import { readFileSync, renameSync, writeFileSync } from 'node:fs';

/** Update preferences live in the writable profile, never inside the signed app. */
export function updatePreferences(filename) {
  let skippedVersion;
  let automaticChecks;
  try {
    const saved = JSON.parse(readFileSync(filename, 'utf8'));
    if (typeof saved?.skippedVersion === 'string') skippedVersion = saved.skippedVersion;
    if (typeof saved?.automaticChecks === 'boolean') automaticChecks = saved.automaticChecks;
  } catch (error) {
    if (error.code !== 'ENOENT') console.warn('Could not read Desktop update preference:', error.message);
  }
  function save(next) {
    const temporary = `${filename}.tmp`;
    writeFileSync(temporary, JSON.stringify(next), { mode: 0o600 });
    renameSync(temporary, filename);
    skippedVersion = next.skippedVersion;
    automaticChecks = next.automaticChecks;
  }
  return {
    getSkippedVersion: () => skippedVersion,
    getAutomaticChecks: () => automaticChecks,
    setAutomaticChecks(enabled) {
      save({ skippedVersion, automaticChecks: enabled });
    },
    skipVersion(version) {
      save({ skippedVersion: version, automaticChecks });
    },
  };
}
